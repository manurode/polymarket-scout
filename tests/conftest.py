import pytest
import sqlite3
import tempfile
import os


@pytest.fixture
def temp_db():
    """SQLite DB temporal para tests."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    yield conn
    conn.close()
    os.unlink(path)


@pytest.fixture
def mock_gamma_response():
    """Respuesta típica de Gamma API /events."""
    return [
        {
            "id": "evt-1",
            "title": "US Election 2024 Winner",
            "slug": "us-election-2024",
            "volume": 5000000.0,
            "active": True,
            "closed": False,
            "markets": [
                {
                    "question": "Will Trump win?",
                    "outcomePrices": '["0.65", "0.35"]',
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["tok-yes-1", "tok-no-1"]',
                    "conditionId": "0xabc123",
                    "volume": 3000000.0,
                    "slug": "trump-win-2024",
                }
            ],
        }
    ]


@pytest.fixture
def mock_clob_price():
    """Respuesta típica de CLOB /price."""
    return {"price": "0.65"}


@pytest.fixture
def mock_clob_spread():
    """Respuesta típica de CLOB /spread."""
    return {"spread": "0.02"}


@pytest.fixture
def config():
    """Config mínima para tests."""
    return {
        "scanner": {"events_limit": 5, "markets_per_event": 3, "min_volume": 5000},
        "tracker": {"db_path": ":memory:", "retention_days": 90},
        "signals": {
            "momentum": {"threshold": 0.05, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000},
        },
        "scorer": {"alert_threshold": 60, "cooldown_minutes": 30},
        "alerter": {"platform": "telegram", "template": "test template"},
    }
