"""Tests para SpoofDetector — detección de spoofing vía OBI-TFI divergence."""

import time
import pytest
from unittest.mock import MagicMock
from src.book_analyzer import BookAnalyzer
from src.trade_aggregator import TradeAggregator
from src.spoof_detector import (
    SpoofDetector,
    SpoofingScore,
    THRESHOLD_NORMAL,
    THRESHOLD_SUSPICIOUS,
    THRESHOLD_PROBABLE,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def book_analyzer():
    ba = BookAnalyzer()
    # Inicializar con un book de ejemplo
    ba.initialize_book("token_001", {
        "bids": [[0.64, 500.0], [0.63, 300.0]],
        "asks": [[0.66, 200.0], [0.67, 100.0]],
    })
    return ba


@pytest.fixture
def trade_aggregator():
    agg = TradeAggregator()
    now = time.time()
    # Añadir algunos trades para tener TFI no nulo
    agg.add_trade("0xabc", {"price": "0.65", "size": "300.0", "side": "BUY", "timestamp": now - 10})
    agg.add_trade("0xabc", {"price": "0.64", "size": "200.0", "side": "SELL", "timestamp": now - 20})
    return agg


@pytest.fixture
def detector(book_analyzer, trade_aggregator):
    return SpoofDetector(book_analyzer, trade_aggregator, window=60)


# ── Constructor ────────────────────────────────────────────────────

def test_detector_initializes(detector):
    assert detector.window == 60
    assert detector.obi_levels == 10


# ── compute_spoofing_score ─────────────────────────────────────────

def test_compute_spoofing_score_returns_valid_result(detector):
    score = detector.compute_spoofing_score("0xabc", "token_001")

    assert isinstance(score, SpoofingScore)
    assert score.condition_id == "0xabc"
    assert score.token_id == "token_001"
    assert -1 <= score.obi <= 1
    assert -1 <= score.tfi <= 1
    assert score.score >= 0  # S ≥ 0 siempre
    assert 0 <= score.confidence_weight <= 1
    assert score.classification in ("normal", "suspicious", "probable", "confirmed")


def test_score_zero_when_obi_equals_tfi(detector, book_analyzer, trade_aggregator):
    """Si OBI == TFI, divergencia = 0 → score = 0."""
    # Crear un mercado donde OBI y TFI coinciden
    ba2 = BookAnalyzer()
    ba2.initialize_book("token_bal", {
        "bids": [[0.5, 600.0]],
        "asks": [[0.6, 400.0]],
    })
    # OBI = (600-400)/1000 = 0.2

    agg2 = TradeAggregator()
    now = time.time()
    # TFI: buy 600, sell 400 → (600-400)/1000 = 0.2
    agg2.add_trade("0xbal", {"price": "0.55", "size": "600", "side": "BUY", "timestamp": now})
    agg2.add_trade("0xbal", {"price": "0.55", "size": "400", "side": "SELL", "timestamp": now})

    det2 = SpoofDetector(ba2, agg2)
    # Necesita suficientes observaciones para confianza
    det2._observation_counts["0xbal"] = 20
    det2._market_first_seen["0xbal"] = now - 3600  # 1h ago

    score = det2.compute_spoofing_score("0xbal", "token_bal")
    assert abs(score.divergence_raw) < 0.01
    assert score.classification == "normal"


def test_score_high_when_obi_diverges_from_tfi(detector, book_analyzer):
    """OBI muy positivo pero TFI muy negativo → alta divergencia → spoofing."""
    # Inicializar book con OBI muy positivo
    book_analyzer.initialize_book("token_sus", {
        "bids": [[0.5, 1000.0], [0.49, 800.0]],
        "asks": [[0.6, 50.0]],
    })
    # OBI = (1800-50)/1850 ≈ 0.946

    # Pero los trades van en dirección contraria
    agg = TradeAggregator()
    now = time.time()
    agg.add_trade("0xsus", {"price": "0.5", "size": "500", "side": "SELL", "timestamp": now})
    # TFI = -1.0

    det = SpoofDetector(book_analyzer, agg)
    # Mercado maduro (>24h) con muchas observaciones → confianza alta
    det._observation_counts["0xsus"] = 20
    det._market_first_seen["0xsus"] = now - 100000  # >24h para confianza plena

    score = det.compute_spoofing_score("0xsus", "token_sus")
    assert score.divergence_raw > 1.5  # |0.946 - (-1.0)| ≈ 1.946
    assert score.score > 0.5  # debería clasificar como al menos "suspicious"


# ── Classification Thresholds ──────────────────────────────────────

def test_classification_normal(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)

    # Simular score bajo
    assert det._classify(0.1) == "normal"
    assert det._classify(0.29) == "normal"


def test_classification_suspicious(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)
    assert det._classify(0.3) == "suspicious"
    assert det._classify(0.49) == "suspicious"


def test_classification_probable(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)
    assert det._classify(0.5) == "probable"
    assert det._classify(0.69) == "probable"


def test_classification_confirmed(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)
    assert det._classify(0.7) == "confirmed"
    assert det._classify(2.0) == "confirmed"


# ── Recommended Actions ────────────────────────────────────────────

def test_recommended_action_normal(detector):
    score = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.1, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="normal",
        timestamp=time.time(),
    )
    action = detector.get_recommended_action(score)
    assert action["action"] == "normal"
    assert action["position_size_multiplier"] == 1.0


def test_recommended_action_reduce_size(detector):
    score = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.4, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="suspicious",
        timestamp=time.time(),
    )
    action = detector.get_recommended_action(score)
    assert action["action"] == "reduce_size"
    assert action["position_size_multiplier"] == 0.75


def test_recommended_action_ignore_obi(detector):
    score = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.6, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="probable",
        timestamp=time.time(),
    )
    action = detector.get_recommended_action(score)
    assert action["action"] == "ignore_obi"
    assert action["position_size_multiplier"] == 0.50


def test_recommended_action_pause(detector):
    score = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.8, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="confirmed",
        timestamp=time.time(),
    )
    action = detector.get_recommended_action(score)
    assert action["action"] == "pause"
    assert action["position_size_multiplier"] == 0.0


# ── Authoritative Direction ────────────────────────────────────────

def test_authoritative_direction_uses_obi_when_clean(book_analyzer, trade_aggregator):
    """Si no hay spoofing, usa OBI como primario."""
    # Book con OBI positivo
    book_analyzer.initialize_book("token_clean", {
        "bids": [[0.5, 800.0]],
        "asks": [[0.6, 200.0]],
    })

    agg = TradeAggregator()
    now = time.time()
    # Pocos trades → baja confianza pero sin divergencia
    agg.add_trade("0xclean", {"price": "0.55", "size": "100", "side": "BUY", "timestamp": now})

    det = SpoofDetector(book_analyzer, agg)
    det._observation_counts["0xclean"] = 20
    det._market_first_seen["0xclean"] = now - 7200

    direction = det.get_authoritative_direction("0xclean", "token_clean")
    assert direction > 0  # debería ser positivo (OBI comprador)


def test_authoritative_direction_uses_tfi_when_spoofing(book_analyzer):
    """Si S ≥ 0.5, usa solo TFI para la dirección."""
    book_analyzer.initialize_book("token_spoof", {
        "bids": [[0.5, 1000.0]],  # OBI muy positivo
        "asks": [[0.6, 50.0]],
    })

    agg = TradeAggregator()
    now = time.time()
    # Pero los trades reales son vendedores
    agg.add_trade("0xspoof", {"price": "0.5", "size": "500", "side": "SELL", "timestamp": now})

    det = SpoofDetector(book_analyzer, agg)
    # Mercado maduro (>24h) + muchas observaciones → score alto → modo TFI
    det._observation_counts["0xspoof"] = 20
    det._market_first_seen["0xspoof"] = now - 100000  # >24h

    direction = det.get_authoritative_direction("0xspoof", "token_spoof")
    # Con alta divergencia y mercado maduro, S ≥ 0.5 → usa TFI que es -1.0
    assert direction < 0


# ── Confidence Weight ──────────────────────────────────────────────

def test_confidence_grows_with_observations(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)
    now = time.time()
    # Mercado maduro (>24h) para que age_factor no penalice
    det._market_first_seen["0xconf"] = now - 100000  # >24h

    # Primera observación: confianza baja (pocas observaciones)
    c1 = det._compute_confidence("0xconf")
    assert c1 < 1.0  # menos de 10 observaciones

    # Después de muchas observaciones + mercado maduro → confianza plena
    det._observation_counts["0xconf"] = 20  # se incrementará a 21 al llamar
    c2 = det._compute_confidence("0xconf")
    assert c2 > c1
    assert abs(c2 - 1.0) < 0.01


def test_new_market_has_low_confidence(book_analyzer, trade_aggregator):
    det = SpoofDetector(book_analyzer, trade_aggregator)
    # Mercado recién visto (hace 1 minuto)
    det._market_first_seen["0xnew"] = time.time() - 60
    det._observation_counts["0xnew"] = 20  # muchas observaciones pero...

    c = det._compute_confidence("0xnew")
    assert c < 0.5  # edad baja → confianza penalizada


# ── Batch ──────────────────────────────────────────────────────────

def test_compute_batch(detector, book_analyzer):
    """Procesa múltiples mercados en lote."""
    book_analyzer.initialize_book("token_b1", {
        "bids": [[0.5, 100.0]], "asks": [[0.6, 100.0]],
    })
    book_analyzer.initialize_book("token_b2", {
        "bids": [[0.5, 200.0]], "asks": [[0.6, 50.0]],
    })

    markets = [("0xabc", "token_001"), ("0xb1", "token_b1"), ("0xb2", "token_b2")]
    results = detector.compute_batch(markets)

    assert len(results) == 3
    assert "0xabc" in results
    assert "0xb1" in results
    assert "0xb2" in results


# ── should_reduce_size ────────────────────────────────────────────

def test_should_reduce_size_returns_multiplier(detector):
    multiplier = detector.should_reduce_size("0xabc")
    assert 0.0 <= multiplier <= 1.0


# ── SpoofingScore helpers ──────────────────────────────────────────

def test_is_spoofing_attribute():
    score = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.6, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="probable",
        timestamp=time.time(),
    )
    assert score.is_spoofing is True
    assert score.requires_pause is False

    score2 = SpoofingScore(
        condition_id="0xabc", token_id="t1",
        score=0.8, obi=0.0, tfi=0.0,
        divergence_raw=0.0, cancel_rate_factor=0.0,
        confidence_weight=1.0, classification="confirmed",
        timestamp=time.time(),
    )
    assert score2.requires_pause is True


# ── Reset / Clear ──────────────────────────────────────────────────

def test_reset_market_clears_history(detector):
    detector._observation_counts["0xabc"] = 50
    detector._market_first_seen["0xabc"] = 100.0

    detector.reset_market("0xabc")
    assert "0xabc" not in detector._observation_counts
    assert "0xabc" not in detector._market_first_seen


def test_clear_resets_all(detector):
    detector._observation_counts["a"] = 1
    detector._market_first_seen["a"] = 1.0
    detector._observation_counts["b"] = 1

    detector.clear()
    assert len(detector._observation_counts) == 0
    assert len(detector._market_first_seen) == 0
