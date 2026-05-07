from src.scorer import calculate_score


def test_calculate_score_all_signals():
    signals = [
        {"signal_type": "momentum_up", "weight": 20, "change_pct": 0.10},
        {"signal_type": "volume_spike", "weight": 20, "ratio": 5.0},
        {"signal_type": "spread_tight", "weight": 15, "spread": 0.01},
    ]
    score, detail = calculate_score(signals)
    # momentum: 20 * min(1, 0.10/0.05) = 20
    # volume: 20 * min(1, 5.0/3.0) = 20
    # spread_tight: 15 * min(1, 0.03/0.01) = 15 → capped at 15
    # total = 55
    assert score == 55
    assert isinstance(detail, str)
    assert "momentum_up" in detail


def test_calculate_score_empty():
    score, detail = calculate_score([])
    assert score == 0
    assert detail == "{}"


def test_calculate_score_capped():
    # Even with extreme intensity, contribution can't exceed weight
    signals = [
        {"signal_type": "momentum_up", "weight": 20, "change_pct": 0.50},
    ]
    score, _ = calculate_score(signals)
    # intensity = min(1, 0.50/0.05) = 1.0, contribution = min(20, 20*1) = 20
    assert score == 20


def test_calculate_score_inverted():
    # spread_tight: lower spread = more intense
    signals = [
        {"signal_type": "spread_tight", "weight": 15, "spread": 0.001},
    ]
    score, _ = calculate_score(signals)
    # intensity = min(1, 0.03/0.001) = 1.0 (capped)
    assert score == 15


def test_calculate_score_unknown_signal_type():
    signals = [
        {"signal_type": "mystery_signal", "weight": 10},
    ]
    score, _ = calculate_score(signals)
    # Unknown type → intensity = 1.0 → contribution = min(10, 10*1) = 10
    assert score == 10


def test_calculate_score_partial_intensity():
    # Below threshold → partial intensity
    signals = [
        {"signal_type": "momentum_up", "weight": 20, "change_pct": 0.025},
    ]
    score, _ = calculate_score(signals)
    # intensity = min(1, 0.025/0.05) = 0.5
    # contribution = int(20 * 0.5) = 10
    assert score == 10


def test_calculate_score_capped_at_100():
    signals = [
        {"signal_type": "momentum_up", "weight": 50, "change_pct": 0.50},
        {"signal_type": "volume_spike", "weight": 50, "ratio": 10.0},
        {"signal_type": "spread_tight", "weight": 50, "spread": 0.001},
    ]
    score, _ = calculate_score(signals)
    assert score <= 100
    assert score == 100


def test_calculate_score_momentum_down():
    signals = [
        {"signal_type": "momentum_down", "weight": 20, "change_pct": -0.10},
    ]
    score, _ = calculate_score(signals)
    # abs(-0.10) / 0.05 = 2.0 → capped at 1.0
    assert score == 20


def test_calculate_score_spread_wide():
    signals = [
        {"signal_type": "spread_wide", "weight": 10, "spread": 0.15},
    ]
    score, _ = calculate_score(signals)
    # intensity = min(1, 0.15/0.10) = 1.0
    assert score == 10


def test_calculate_score_new_interest():
    signals = [
        {"signal_type": "new_interest", "weight": 10, "volume": 50000},
    ]
    score, _ = calculate_score(signals)
    # intensity = min(1, 50000/10000) = 1.0
    assert score == 10
