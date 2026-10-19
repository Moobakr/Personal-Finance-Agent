"""Stock transport and completed-session selection; no network or yfinance needed."""
import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import requests

from finance_agent.api import ServiceError, fetch_stock_price
from finance_agent.storage import DataError


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


NOW = utc("2026-01-05T18:00:00")


def chart_payload():
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": "AAPL", "currency": "USD", "exchangeTimezoneName": "America/New_York",
                 "gmtoffset": -18000, "currentTradingPeriod": {"regular": {
                     "start": utc("2026-01-05T14:30:00").timestamp(),
                     "end": utc("2026-01-05T21:00:00").timestamp()}}},
        "timestamp": [utc("2026-01-02T14:30:00").timestamp(), utc("2026-01-05T14:30:00").timestamp()],
        "indicators": {"quote": [{"close": [120.25, 123.45]}]},
    }]}}


class StockTests(unittest.TestCase):
    def setUp(self):
        self.payload = chart_payload()
        self.session = Mock()
        self.session.get.return_value.ok = True
        self.session.get.return_value.json.side_effect = lambda: self.payload

    @property
    def chart(self):
        return self.payload["chart"]["result"][0]

    def fetch(self, symbol="AAPL", **kwargs):
        return fetch_stock_price(symbol, session=self.session, now=kwargs.get("now", NOW))

    def test_public_chart_request_and_no_optional_package(self):
        with patch.dict("sys.modules", {"yfinance": None}):
            result = self.fetch(" aapl ")
        self.assertEqual(result["ticker"], "AAPL")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["source"], "Yahoo Finance")
        self.session.get.assert_called_once()
        args, kwargs = self.session.get.call_args
        self.assertTrue(args[0].endswith("/AAPL"))
        self.assertEqual(kwargs["params"], {"range": "5d", "interval": "1d"})
        self.assertEqual(kwargs["timeout"], (5, 15))
        self.assertNotIn("Authorization", kwargs["headers"])
        self.session.close.assert_not_called()  # Caller owns an injected session.

    def test_open_market_does_not_label_partial_bar_as_daily_close(self):
        result = self.fetch()
        self.assertEqual(result["close"], 120.25)
        self.assertEqual(result["market_date"], "2026-01-02")

    def test_completed_session_includes_today_after_market_close(self):
        result = self.fetch(now=utc("2026-01-05T21:05:00"))
        self.assertEqual(result["close"], 123.45)
        self.assertEqual(result["market_date"], "2026-01-05")
        self.assertEqual(result["retrieved_at"], "2026-01-05T21:05:00+00:00")

    def test_exchange_date_uses_local_timezone_across_utc_midnight(self):
        result = self.fetch(now=utc("2026-01-06T00:15:00"))
        self.assertEqual(result["market_date"], "2026-01-05")

    def test_asian_market_hours_are_respected(self):
        self.chart["meta"].update(exchangeTimezoneName="Asia/Tokyo", gmtoffset=32400)
        self.chart["meta"]["currentTradingPeriod"]["regular"] = {
            "start": utc("2026-01-05T00:00:00").timestamp(), "end": utc("2026-01-05T06:30:00").timestamp()}
        self.chart["timestamp"] = [utc("2026-01-02T00:00:00").timestamp(), utc("2026-01-05T00:00:00").timestamp()]
        self.assertEqual(self.fetch(now=utc("2026-01-05T05:00:00"))["market_date"], "2026-01-02")
        self.assertEqual(self.fetch(now=utc("2026-01-05T07:00:00"))["market_date"], "2026-01-05")

    def test_timezone_fallback_and_pence_unit_preserved(self):
        self.chart["meta"]["exchangeTimezoneName"] = "Not/ARealZone"
        self.chart["meta"]["currency"] = "GBp"
        result = self.fetch()
        self.assertEqual(result["currency"], "GBp")
        self.assertEqual(result["market_date"], "2026-01-02")

    def test_invalid_latest_values_are_skipped(self):
        for value in [None, float("nan"), float("inf"), -1, 0, True, "123"]:
            with self.subTest(value=value):
                self.chart["indicators"]["quote"][0]["close"][1] = value
                self.assertEqual(self.fetch(now=utc("2026-01-05T22:00:00"))["close"], 120.25)

    def test_future_bar_is_not_used(self):
        self.chart["timestamp"][1] = utc("2026-01-06T14:30:00").timestamp()
        self.assertEqual(self.fetch()["close"], 120.25)

    def test_missing_trading_hours_keeps_only_older_confirmed_close(self):
        self.chart["meta"].pop("currentTradingPeriod")
        self.assertEqual(self.fetch()["market_date"], "2026-01-02")

    def test_no_completed_bar_has_clear_error(self):
        self.chart["timestamp"] = self.chart["timestamp"][1:]
        self.chart["indicators"]["quote"][0]["close"] = [123.45]
        with self.assertRaisesRegex(ServiceError, "No completed daily closing price"):
            self.fetch()

    def test_invalid_symbols_are_rejected_before_network(self):
        for symbol in [None, 123, "", " ", "AAPL MSFT", "../AAPL", "https://example.com", "A" * 21, "AAPL?key=x"]:
            with self.subTest(symbol=symbol), self.assertRaises(DataError):
                self.fetch(symbol)
        self.session.get.assert_not_called()

    def test_exchange_symbols_are_encoded_as_one_url_component(self):
        result = self.fetch("^GSPC")
        self.assertEqual(result["ticker"], "^GSPC")
        self.assertTrue(self.session.get.call_args.args[0].endswith("/%5EGSPC"))
        self.fetch("BRK-B")
        self.assertTrue(self.session.get.call_args.args[0].endswith("/BRK-B"))

    def test_http_errors_are_actionable(self):
        for status, message in [(404, "Ticker not found"), (429, "rate limiting"),
                                (401, "denied"), (403, "blocked"), (503, "HTTP 503")]:
            with self.subTest(status=status):
                self.session.get.return_value.ok = False
                self.session.get.return_value.status_code = status
                with self.assertRaisesRegex(ServiceError, message):
                    self.fetch()

    def test_timeouts_and_connection_errors_are_distinct(self):
        for error, message in [(requests.Timeout(), "timed out"), (requests.ConnectionError(), "Could not reach")]:
            with self.subTest(message=message):
                self.session.get.side_effect = error
                with self.assertRaisesRegex(ServiceError, message):
                    self.fetch()

    def test_bad_json_missing_currency_and_incomplete_arrays(self):
        self.session.get.return_value.json.side_effect = ValueError("bad JSON")
        with self.assertRaisesRegex(ServiceError, "invalid price data"):
            self.fetch()
        self.session.get.return_value.json.side_effect = lambda: self.payload
        original = copy.deepcopy(self.payload)
        self.chart["meta"].pop("currency")
        with self.assertRaisesRegex(ServiceError, "invalid price data"):
            self.fetch()
        self.payload = original
        self.chart["indicators"]["quote"][0]["close"] = [120.25]
        with self.assertRaisesRegex(ServiceError, "invalid price data"):
            self.fetch()

    def test_provider_error_payload_is_not_rendered_verbatim(self):
        self.payload = {"chart": {"result": None, "error": {"description": "untrusted provider details"}}}
        with self.assertRaisesRegex(ServiceError, "Check its Yahoo Finance symbol") as caught:
            self.fetch()
        self.assertNotIn("untrusted provider details", str(caught.exception))

    def test_owned_session_is_closed_on_success_and_failure(self):
        with patch("finance_agent.api.requests.Session", return_value=self.session):
            fetch_stock_price("AAPL", now=NOW)
            self.session.close.assert_called_once()
            self.session.close.reset_mock()
            self.session.get.side_effect = requests.Timeout()
            with self.assertRaises(ServiceError):
                fetch_stock_price("AAPL", now=NOW)
            self.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
