"""
Polymarket CLOB API authentication — HMAC-SHA256 signing.

For every authenticated REST request to clob.polymarket.com, we build
three headers: POLY_API_KEY, POLY_TIMESTAMP, POLY_SIGNATURE.

Reference: https://docs.polymarket.com/api/clob/authentication
"""

import base64
import hashlib
import hmac
import time


def build_clob_headers(
    api_key: str,
    secret: str,
    method: str,
    request_path: str,
    body: str = "",
) -> dict[str, str]:
    """Build authentication headers for Polymarket CLOB REST API.

    Args:
        api_key:  CLOB API key (from .env → CLOB_API_KEY)
        secret:   CLOB API secret (from .env → CLOB_SECRET)
        method:   HTTP method — GET, POST, DELETE
        request_path: URL path including query string.
                      Example: "/book?token_id=123..."
        body:     Request body for POST/PUT/DELETE.
                  Empty string for GET requests.

    Returns:
        dict with keys: POLY_API_KEY, POLY_TIMESTAMP, POLY_SIGNATURE
    """
    timestamp = str(int(time.time()))

    # Canonical string to sign: timestamp + method + path + body
    signable = timestamp + method.upper() + request_path + body

    # HMAC-SHA256 with binary secret, base64-encoded
    signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            signable.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    return {
        "POLY_API_KEY": api_key,
        "POLY_TIMESTAMP": timestamp,
        "POLY_SIGNATURE": signature,
    }


def build_clob_auth_message(
    api_key: str,
    secret: str,
    passphrase: str,
) -> dict:
    """Build WebSocket auth message for CLOB WS subscriptions.

    Some CLOB WebSocket endpoints require an initial auth message
    after connecting. This builds the standard Polymarket auth message.

    Args:
        api_key:    CLOB API key
        secret:     CLOB API secret
        passphrase: CLOB API passphrase

    Returns:
        dict ready to send as JSON over WebSocket:
        {"type": "auth", "apiKey": "...", "secret": "...", "passphrase": "..."}
    """
    return {
        "type": "auth",
        "apiKey": api_key,
        "secret": secret,
        "passphrase": passphrase,
    }
