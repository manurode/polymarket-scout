"""Tests for PolymarketScanner."""
import pytest
from unittest.mock import patch, Mock, MagicMock

from src.scanner import PolymarketScanner


@pytest.fixture
def scanner():
    """Fresh scanner instance for each test."""
    return PolymarketScanner()


class TestGetRequest:
    """Tests for the _get() HTTP method."""

    def test_get_makes_request_and_returns_parsed_json(self, scanner):
        """Verify _get makes a urllib request and returns parsed JSON."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            # Set up the context manager chain: urlopen().__enter__().read()
            mock_resp = Mock()
            mock_resp.read.return_value = b'{"key": "value"}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            result = scanner._get("https://gamma-api.polymarket.com/events?limit=1")

        assert result == {"key": "value"}
        mock_urlopen.assert_called_once()

    def test_get_sets_user_agent_header(self, scanner):
        """Verify _get sends the configured User-Agent header."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = Mock()
            mock_resp.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            scanner._get("https://example.com/api")

            # Verify the Request was created with the right headers
            call_args = mock_urlopen.call_args
            req = call_args[0][0]  # first positional arg to urlopen
            assert req.get_header("User-agent") == "polymarket-scout/1.0"


class TestParseJsonField:
    """Tests for parse_json_field static method."""

    def test_parses_double_encoded_json_string(self):
        """String containing JSON should be parsed."""
        result = PolymarketScanner.parse_json_field('["0.65", "0.35"]')
        assert result == ["0.65", "0.35"]

    def test_passes_through_non_string_values(self):
        """Non-string values should be returned as-is."""
        assert PolymarketScanner.parse_json_field(42) == 42
        assert PolymarketScanner.parse_json_field([1, 2, 3]) == [1, 2, 3]
        assert PolymarketScanner.parse_json_field(None) is None

    def test_returns_original_on_invalid_json(self):
        """Invalid JSON strings should be returned as-is."""
        result = PolymarketScanner.parse_json_field("not valid json at all")
        assert result == "not valid json at all"


class TestGetEvents:
    """Tests for get_events()."""

    def test_get_events_returns_list(self, scanner, mock_gamma_response):
        """get_events should return a list of event dicts."""
        with patch.object(scanner, "_get", return_value=mock_gamma_response):
            events = scanner.get_events(limit=10)

        assert isinstance(events, list)
        assert len(events) == 1
        assert events[0]["title"] == "US Election 2024 Winner"

    def test_get_events_passes_active_filter(self, scanner):
        """When active_only=True, the URL should include active=true."""
        with patch.object(scanner, "_get", return_value=[]) as mock_get:
            scanner.get_events(limit=5, active_only=True)

        call_url = mock_get.call_args[0][0]
        assert "active=true" in call_url
        assert "closed=false" in call_url

    def test_get_events_respects_limit(self, scanner):
        """The limit parameter should appear in the URL."""
        with patch.object(scanner, "_get", return_value=[]) as mock_get:
            scanner.get_events(limit=42)

        call_url = mock_get.call_args[0][0]
        assert "limit=42" in call_url


class TestGetPrice:
    """Tests for get_price()."""

    def test_get_price_returns_float(self, scanner, mock_clob_price):
        """get_price should return a float parsed from the 'price' field."""
        with patch.object(scanner, "_get", return_value=mock_clob_price):
            price = scanner.get_price("tok-yes-1", side="buy")

        assert price == 0.65
        assert isinstance(price, float)

    def test_get_price_with_string_value(self, scanner):
        """Price returned as string should be converted to float."""
        with patch.object(scanner, "_get", return_value={"price": "0.42"}):
            price = scanner.get_price("tok")

        assert price == 0.42
        assert isinstance(price, float)

    def test_get_price_uses_correct_url(self, scanner):
        """The URL should contain token_id and side parameters."""
        with patch.object(scanner, "_get", return_value={"price": "0.5"}) as mock_get:
            scanner.get_price("abc123", side="sell")

        call_url = mock_get.call_args[0][0]
        assert "token_id=abc123" in call_url
        assert "side=sell" in call_url


class TestGetSpread:
    """Tests for get_spread()."""

    def test_get_spread_returns_float(self, scanner, mock_clob_spread):
        """get_spread should return a float from the 'spread' field."""
        with patch.object(scanner, "_get", return_value=mock_clob_spread):
            spread = scanner.get_spread("tok-yes-1")

        assert spread == 0.02
        assert isinstance(spread, float)

    def test_get_spread_uses_correct_url(self, scanner):
        """The URL should contain the token_id."""
        with patch.object(scanner, "_get", return_value={"spread": "0.01"}) as mock_get:
            scanner.get_spread("xyz789")

        call_url = mock_get.call_args[0][0]
        assert "token_id=xyz789" in call_url


class TestScanMarkets:
    """Tests for scan_markets() — the main orchestration method."""

    def test_scan_markets_returns_snapshots(
        self, scanner, mock_gamma_response, mock_clob_price, mock_clob_spread
    ):
        """scan_markets should return a list of snapshot dicts with correct structure."""
        with patch.object(scanner, "_get") as mock_get:
            # Order of calls:
            # 1. get_events   → gamma response
            # 2. get_price    → for token-yes-1
            # 3. get_spread   → for token-yes-1
            mock_get.side_effect = [
                mock_gamma_response,
                mock_clob_price,
                mock_clob_spread,
            ]

            snapshots = scanner.scan_markets(
                events_limit=1, markets_per_event=5, min_volume=0
            )

        assert len(snapshots) == 1
        s = snapshots[0]

        # Verify all required keys
        assert s["condition_id"] == "0xabc123"
        assert s["question"] == "Will Trump win?"
        assert s["slug"] == "trump-win-2024"
        assert s["event_title"] == "US Election 2024 Winner"
        assert s["price_yes"] == 0.65
        assert s["price_no"] == pytest.approx(0.35, abs=0.01)
        assert s["spread"] == 0.02
        assert s["volume"] == 3000000.0
        assert isinstance(s["timestamp"], int)

    def test_scan_markets_respects_min_volume(self, scanner):
        """Markets below min_volume should be excluded."""
        low_volume_event = [
            {
                "id": "evt-1",
                "title": "Low Vol Event",
                "slug": "low-vol",
                "volume": 100,
                "active": True,
                "closed": False,
                "markets": [
                    {
                        "question": "Tiny market",
                        "outcomePrices": '["0.50", "0.50"]',
                        "outcomes": '["Yes", "No"]',
                        "clobTokenIds": '["tok-a", "tok-b"]',
                        "conditionId": "0xlow",
                        "volume": 10.0,
                        "slug": "tiny",
                    }
                ],
            }
        ]

        with patch.object(scanner, "_get", return_value=low_volume_event):
            snapshots = scanner.scan_markets(
                events_limit=1, markets_per_event=5, min_volume=5000
            )

        # Low volume market should be filtered out
        assert len(snapshots) == 0

    def test_scan_markets_respects_markets_per_event(self, scanner):
        """Should only include up to markets_per_event markets per event."""
        multi_market_event = [
            {
                "id": "evt-multi",
                "title": "Multi Market Event",
                "slug": "multi",
                "volume": 100000,
                "active": True,
                "closed": False,
                "markets": [
                    {
                        "question": f"Market {i}",
                        "outcomePrices": '["0.50", "0.50"]',
                        "outcomes": '["Yes", "No"]',
                        "clobTokenIds": f'["tok-{i}-yes", "tok-{i}-no"]',
                        "conditionId": f"0x{i}",
                        "volume": 100000.0,
                        "slug": f"market-{i}",
                    }
                    for i in range(5)
                ],
            }
        ]

        with patch.object(scanner, "_get") as mock_get:
            # 1 event call + (3 calls per market × 2 markets) = 7 calls
            mock_get.side_effect = [
                multi_market_event,
                {"price": "0.50"}, {"spread": "0.01"},
                {"price": "0.50"}, {"spread": "0.01"},
                {"price": "0.50"}, {"spread": "0.01"},
            ]

            snapshots = scanner.scan_markets(
                events_limit=1, markets_per_event=2, min_volume=0
            )

        assert len(snapshots) == 2
        assert snapshots[0]["slug"] == "market-0"
        assert snapshots[1]["slug"] == "market-1"

    def test_scan_markets_skips_inactive_events(self, scanner):
        """Inactive events should be filtered by the API (active=true param)."""
        # The active filtering is done by the Gamma API via the query params.
        # We verify that get_events is called with active_only=True.
        with patch.object(scanner, "_get", return_value=[]) as mock_get:
            scanner.scan_markets(events_limit=25)

        # Verify the URL passed to _get contains active=true
        call_url = mock_get.call_args[0][0]
        assert "active=true" in call_url
