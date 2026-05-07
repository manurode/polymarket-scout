"""
SpoofDetector — Detección de spoofing vía divergencia OBI vs TFI.

El spoofing consiste en colocar órdenes límite grandes sin intención de
ejecutarlas, inflando artificialmente el OBI sin dejar huella en el TFI.
La divergencia OBI–TFI es la huella dactilar del spoofing.

Uso:
    detector = SpoofDetector(book_analyzer, trade_aggregator)
    score = detector.compute_spoofing_score(condition_id)
    if score >= 0.5:
        print("Posible spoofing detectado")
"""

import logging
import time
from dataclasses import dataclass

from src.book_analyzer import BookAnalyzer
from src.trade_aggregator import TradeAggregator

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

AVG_CANCEL_RATE = 5              # cancelaciones grandes "normales" por minuto
MIN_OBSERVATIONS = 10            # mínimo de observaciones para confianza plena
FULL_CONFIDENCE_WINDOW = 300     # segundos para confianza plena (5 min)
MARKET_AGE_HOURS_NEW = 24        # mercados con < 24h se consideran "nuevos"

# Umbrales de acción (del ARCHITECTURE_V2.md)
THRESHOLD_NORMAL = 0.3           # S < 0.3 → sin acción
THRESHOLD_SUSPICIOUS = 0.5       # 0.3 ≤ S < 0.5 → reducir size al 75%
THRESHOLD_PROBABLE = 0.7         # 0.5 ≤ S < 0.7 → ignorar OBI, size 50%
# S ≥ 0.7                        # → spoofing confirmado, pausar trading


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class SpoofingScore:
    """Resultado del cálculo de spoofing."""
    condition_id: str
    token_id: str
    score: float               # Spoofing Score S en [0, ∞)
    obi: float                 # Order Book Imbalance en [-1, +1]
    tfi: float                 # Trade Flow Imbalance en [-1, +1]
    divergence_raw: float      # |OBI - TFI| sin ajustar
    cancel_rate_factor: float  # factor de cancelaciones grandes
    confidence_weight: float   # peso de confianza [0, 1]
    classification: str        # "normal", "suspicious", "probable", "confirmed"
    timestamp: float

    @property
    def is_spoofing(self) -> bool:
        """Conveniencia: True si S ≥ 0.5 (probable o confirmado)."""
        return self.score >= THRESHOLD_SUSPICIOUS

    @property
    def requires_pause(self) -> bool:
        """True si se requiere pausar trading (S ≥ 0.7)."""
        return self.score >= THRESHOLD_PROBABLE


# ── SpoofDetector ─────────────────────────────────────────────────

class SpoofDetector:
    """Detector de spoofing mediante divergencia OBI–TFI.

    Parameters
    ----------
    book_analyzer : BookAnalyzer
        Analizador de order books con OBI en tiempo real.
    trade_aggregator : TradeAggregator
        Agregador de trades con TFI por buckets.
    window : float
        Ventana para calcular TFI (default 60s).
    obi_levels : int
        Niveles del libro para OBI (default 10).
    """

    def __init__(
        self,
        book_analyzer: BookAnalyzer,
        trade_aggregator: TradeAggregator,
        window: float = 60,
        obi_levels: int = 10,
    ):
        self._books = book_analyzer
        self._trades = trade_aggregator
        self.window = window
        self.obi_levels = obi_levels

        # Historial para tracking de confianza
        self._observation_counts: dict[str, int] = {}
        self._market_first_seen: dict[str, float] = {}

    # ── Core Calculation ──────────────────────────────────────────

    def compute_spoofing_score(
        self,
        condition_id: str,
        token_id: str | None = None,
    ) -> SpoofingScore:
        """Calcula el Spoofing Score completo para un mercado.

        Parameters
        ----------
        condition_id : str
            Identificador del mercado.
        token_id : str | None
            Token ID asociado. Si es None, se usa condition_id como fallback.

        Returns
        -------
        SpoofingScore
        """
        tid = token_id or condition_id
        now = time.time()

        # ── 1. Obtener OBI del BookAnalyzer ────────────────────
        obi = self._books.get_obi(tid, levels=self.obi_levels)

        # ── 2. Obtener TFI del TradeAggregator ─────────────────
        tfi_result = self._trades.get_tfi(condition_id, window=self.window)
        tfi = tfi_result.tfi

        # ── 3. Divergencia bruta ───────────────────────────────
        d_raw = abs(obi - tfi)

        # ── 4. Cancel Rate Factor ──────────────────────────────
        large_cancels = self._books.get_large_cancellations(tid)
        cancel_rate_factor = min(1.0, large_cancels / max(1, AVG_CANCEL_RATE))

        # ── 5. Divergencia ajustada ────────────────────────────
        d_adjusted = d_raw * (1.0 + cancel_rate_factor)

        # ── 6. Confidence Weight ───────────────────────────────
        confidence = self._compute_confidence(condition_id)

        # ── 7. Spoofing Score final ────────────────────────────
        s = d_adjusted * confidence

        # ── 8. Clasificación ───────────────────────────────────
        classification = self._classify(s)

        return SpoofingScore(
            condition_id=condition_id,
            token_id=tid,
            score=s,
            obi=obi,
            tfi=tfi,
            divergence_raw=d_raw,
            cancel_rate_factor=cancel_rate_factor,
            confidence_weight=confidence,
            classification=classification,
            timestamp=now,
        )

    def compute_batch(
        self,
        markets: list[tuple[str, str]],
    ) -> dict[str, SpoofingScore]:
        """Calcula Spoofing Score para múltiples mercados.

        Parameters
        ----------
        markets : list[tuple[str, str]]
            Lista de (condition_id, token_id) o solo condition_id.

        Returns
        -------
        dict[str, SpoofingScore]
            condition_id → score.
        """
        results = {}
        for item in markets:
            if isinstance(item, tuple):
                cid, tid = item
            else:
                cid, tid = item, None
            results[cid] = self.compute_spoofing_score(cid, tid)
        return results

    # ── Action Thresholds ─────────────────────────────────────────

    def get_recommended_action(self, score: SpoofingScore) -> dict:
        """Determina la acción recomendada según el Spoofing Score.

        Returns
        -------
        dict con:
            - action: "normal" | "reduce_size" | "ignore_obi" | "pause"
            - position_size_multiplier: float (0.0 a 1.0)
            - description: str
        """
        s = score.score

        if s < THRESHOLD_NORMAL:
            return {
                "action": "normal",
                "position_size_multiplier": 1.0,
                "description": "Sin acción — operar normal",
            }
        elif s < THRESHOLD_SUSPICIOUS:
            return {
                "action": "reduce_size",
                "position_size_multiplier": 0.75,
                "description": "Sospechoso — reducir size al 75%",
            }
        elif s < THRESHOLD_PROBABLE:
            return {
                "action": "ignore_obi",
                "position_size_multiplier": 0.50,
                "description": "Probable spoofing — ignorar OBI, size al 50%",
            }
        else:
            return {
                "action": "pause",
                "position_size_multiplier": 0.0,
                "description": "Spoofing confirmado — pausar trading",
            }

    def should_reduce_size(self, condition_id: str) -> float:
        """Retorna el multiplicador de position size recomendado (1.0 = normal)."""
        score = self.compute_spoofing_score(condition_id)
        return self.get_recommended_action(score)["position_size_multiplier"]

    # ── Signal Decision ──────────────────────────────────────────

    def get_authoritative_direction(
        self, condition_id: str, token_id: str | None = None
    ) -> float:
        """Determina la dirección de trading autoritativa.

        Si S ≥ 0.5, se ignora OBI y se usa solo TFI.
        Si S < 0.5, se usa OBI como primario con TFI de confirmación.

        Returns
        -------
        float
            Señal direccional: +1 (comprar YES), -1 (vender YES), 0 (neutro).
        """
        score = self.compute_spoofing_score(condition_id, token_id)

        if score.score >= THRESHOLD_SUSPICIOUS:
            # Usar solo TFI (volumen comprador real)
            if score.tfi > 0.2:
                return 1.0
            elif score.tfi < -0.2:
                return -1.0
            return 0.0
        else:
            # OBI primario, TFI confirmación
            signal = 0.0
            if score.obi > 0.15:
                signal = 1.0
            elif score.obi < -0.15:
                signal = -1.0

            # TFI confirma o atenúa
            if signal > 0 and score.tfi < -0.1:
                signal *= 0.5  # TFI contradice → señal más débil
            elif signal < 0 and score.tfi > 0.1:
                signal *= 0.5

            return signal

    # ── Internal ──────────────────────────────────────────────────

    def _compute_confidence(self, condition_id: str) -> float:
        """Calcula el peso de confianza basado en observaciones acumuladas.

        Menos observaciones → menor confianza.
        Mercados muy nuevos → penalización adicional.
        """
        now = time.time()

        # Tracking de observaciones
        count = self._observation_counts.get(condition_id, 0) + 1
        self._observation_counts[condition_id] = count

        # Tracking de primera vez visto
        if condition_id not in self._market_first_seen:
            self._market_first_seen[condition_id] = now
        age = now - self._market_first_seen[condition_id]

        # Factor de observaciones: crece con el número de observaciones
        obs_factor = min(1.0, count / MIN_OBSERVATIONS)

        # Factor de edad del mercado: penaliza mercados muy nuevos
        age_hours = age / 3600
        if age_hours < MARKET_AGE_HOURS_NEW:
            age_factor = max(0.2, age_hours / MARKET_AGE_HOURS_NEW)
        else:
            age_factor = 1.0

        return obs_factor * age_factor

    def _classify(self, score: float) -> str:
        """Clasifica el Spoofing Score."""
        if score < THRESHOLD_NORMAL:
            return "normal"
        elif score < THRESHOLD_SUSPICIOUS:
            return "suspicious"
        elif score < THRESHOLD_PROBABLE:
            return "probable"
        else:
            return "confirmed"

    def reset_market(self, condition_id: str) -> None:
        """Resetea el historial de tracking para un mercado."""
        self._observation_counts.pop(condition_id, None)
        self._market_first_seen.pop(condition_id, None)

    def clear(self) -> None:
        """Resetea todo el estado."""
        self._observation_counts.clear()
        self._market_first_seen.clear()
