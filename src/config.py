"""
Centralized configuration loader for Polymarket Scout.

Loads from .env file (API keys) and config.yaml (strategy params).
All modules import get_clob_credentials() / get_yaml_config() from here.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# ── Project root ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Load .env ───────────────────────────────────────────────────
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# ── Load YAML ───────────────────────────────────────────────────
_yaml_path = PROJECT_ROOT / "config.yaml"
_yaml_config: dict[str, Any] = {}
if _yaml_path.exists():
    with open(_yaml_path) as f:
        _yaml_config = yaml.safe_load(f) or {}


# ── Public API ──────────────────────────────────────────────────

def get_clob_credentials() -> dict[str, str]:
    """Return CLOB API credentials from environment (.env)."""
    return {
        "api_key": os.getenv("CLOB_API_KEY", ""),
        "secret": os.getenv("CLOB_SECRET", ""),
        "passphrase": os.getenv("CLOB_PASSPHRASE", ""),
    }


def has_clob_credentials() -> bool:
    """Check if all 3 CLOB API credentials are configured."""
    creds = get_clob_credentials()
    return bool(creds["api_key"] and creds["secret"] and creds["passphrase"])


def get_wallet_credentials() -> dict[str, str]:
    """Return Polygon wallet credentials from environment."""
    return {
        "address": os.getenv("POLYGON_ADDRESS", ""),
        "private_key": os.getenv("POLYGON_PRIVATE_KEY", ""),
    }


def get_yaml_config() -> dict[str, Any]:
    """Return the full YAML configuration dict."""
    return _yaml_config


def get(key: str, default: Any = None) -> Any:
    """Get a nested value from YAML config by dot-notation key.

    Example:
        get("auto_trader.max_open_positions") → 10
        get("radar.interval_seconds", 30) → 60
    """
    parts = key.split(".")
    value: Any = _yaml_config
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
            if value is None:
                return default
        else:
            return default
    return value
