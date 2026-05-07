import pytest
import tempfile
import os


@pytest.fixture
def temp_db():
    """SQLite DB temporal para tests."""
    import sqlite3
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
    """Config mínima para tests v2.0."""
    return {
        "scanner": {"events_limit": 5, "markets_per_event": 3, "min_volume": 5000},
        "selection": {"top_n": 50},
        "rate_limiter": {"total_rate": 100.0, "max_burst": 100.0},
        "radar": {"interval_seconds": 60},
    }
