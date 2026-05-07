import json
from src.alerter import format_alert, should_alert, dispatch_alerts


def test_should_alert_above():
    assert should_alert(score=75, threshold=60) is True


def test_should_alert_below():
    assert should_alert(score=45, threshold=60) is False


def test_should_alert_equal():
    assert should_alert(score=60, threshold=60) is True


def test_format_alert_contains_score():
    snapshot = {
        "question": "Will X happen?",
        "event_title": "Test Event",
        "price_yes": 0.65,
        "volume": 5000000,
        "spread": 0.02,
        "slug": "test-slug",
    }
    detail = json.dumps([
        {"signal_type": "momentum_up", "weight": 20, "intensity": 1.0, "contribution": 20},
    ])
    msg = format_alert(55, snapshot, detail, "+12.0")
    assert "55/100" in msg


def test_format_alert_contains_question():
    snapshot = {
        "question": "Will Trump win 2024?",
        "event_title": "US Election 2024",
        "price_yes": 0.65,
        "volume": 5000000,
        "spread": 0.02,
        "slug": "trump-2024",
    }
    detail = json.dumps([
        {"signal_type": "momentum_up", "weight": 20, "intensity": 1.0, "contribution": 20},
    ])
    msg = format_alert(55, snapshot, detail, "+12.0%")
    assert "Will Trump win 2024?" in msg


def test_format_alert_contains_signals():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "slug",
    }
    detail = json.dumps([
        {"signal_type": "momentum_up", "weight": 20, "intensity": 1.0, "contribution": 20},
        {"signal_type": "volume_spike", "weight": 20, "intensity": 0.8, "contribution": 16},
    ])
    msg = format_alert(55, snapshot, detail, "+12.0")
    assert "momentum_up" in msg
    assert "volume_spike" in msg


def test_format_alert_momentum_positive():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "s",
    }
    detail = json.dumps([])
    msg = format_alert(55, snapshot, detail, "12.0")
    assert "+12.0%" in msg


def test_format_alert_momentum_negative():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "s",
    }
    detail = json.dumps([])
    msg = format_alert(55, snapshot, detail, "-5.0")
    assert "-5.0%" in msg


def test_format_alert_without_slug():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "",
    }
    detail = json.dumps([])
    msg = format_alert(55, snapshot, detail)
    assert "polymarket.com" not in msg


def test_format_alert_default_momentum():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "s",
    }
    msg = format_alert(55, snapshot, "{}")
    assert "—" in msg  # default placeholder


def test_format_alert_invalid_detail_json():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.60,
        "volume": 100000,
        "spread": 0.02,
        "slug": "s",
    }
    msg = format_alert(55, snapshot, "not valid json")
    # Should not crash
    assert "55/100" in msg


def test_format_alert_missing_fields():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "slug": "s",
    }
    msg = format_alert(55, snapshot, "{}")
    assert "Q?" in msg
    assert "—" in msg  # price defaults to —


def test_format_alert_volume_formatting():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.50,
        "volume": 1500000,
        "spread": 0.03,
        "slug": "s",
    }
    msg = format_alert(55, snapshot, "{}")
    assert "$1.5M" in msg


def test_dispatch_alerts_returns_count():
    alerts = ["alert 1", "alert 2", "alert 3"]
    count = dispatch_alerts(alerts)
    assert count == 3


def test_dispatch_alerts_empty():
    count = dispatch_alerts([])
    assert count == 0


def test_format_alert_emoji_for_signal_types():
    snapshot = {
        "question": "Q?",
        "event_title": "E",
        "price_yes": 0.50,
        "volume": 100000,
        "spread": 0.02,
        "slug": "s",
    }
    detail = json.dumps([
        {"signal_type": "momentum_down", "contribution": 15},
        {"signal_type": "new_interest", "contribution": 10},
    ])
    msg = format_alert(60, snapshot, detail)
    assert "📉" in msg
    assert "🆕" in msg
