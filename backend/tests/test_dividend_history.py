"""#1083: the public dividend-history fallback source. No network — httpx is
monkeypatched at the module boundary (httpx.get)."""

import httpx
import pytest

from backend.dividend_history import PublicDividendError, fetch_public_dividends

_OK_CHART = {
    "chart": {
        "result": [
            {
                "timestamp": [1, 2, 3],
                "events": {
                    "dividends": {
                        "1655904600": {"amount": 0.142, "date": 1655904600},
                        "1638973800": {"amount": 0.168, "date": 1638973800},
                    }
                },
            }
        ]
    }
}

_NO_DIVIDENDS_CHART = {"chart": {"result": [{"timestamp": [1, 2, 3]}]}}

_UNRESOLVED_CHART = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}

_NO_BARS_CHART = {"chart": {"result": [{"meta": {"symbol": "ZZZZ"}}]}}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


class TestFetchPublicDividends:
    def test_parses_and_sorts_oldest_first(self, monkeypatch):
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp(_OK_CHART))
        divs = fetch_public_dividends("SCHH")
        assert [(d.ex_date, d.amount_per_share) for d in divs] == [
            ("2021-12-08", 0.168),
            ("2022-06-22", 0.142),
        ]
        assert all(d.symbol == "SCHH" for d in divs)

    def test_a_resolved_symbol_that_pays_nothing_is_an_empty_list_not_a_failure(self, monkeypatch):
        # IAUM: a gold trust, legitimately zero income — must not look like a fetch failure.
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp(_NO_DIVIDENDS_CHART))
        assert fetch_public_dividends("IAUM") == []

    def test_an_unresolved_symbol_is_none_not_empty(self, monkeypatch):
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp(_UNRESOLVED_CHART))
        assert fetch_public_dividends("NOPE") is None

    def test_a_response_shape_with_no_price_bars_is_also_unresolved(self, monkeypatch):
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp(_NO_BARS_CHART))
        assert fetch_public_dividends("ZZZZ") is None

    def test_an_http_error_raises_public_dividend_error(self, monkeypatch):
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp({}, status=500))
        with pytest.raises(PublicDividendError):
            fetch_public_dividends("SCHB")

    def test_a_network_error_raises_public_dividend_error(self, monkeypatch):
        def _boom(*a, **kw):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr("httpx.get", _boom)
        with pytest.raises(PublicDividendError):
            fetch_public_dividends("SCHB")

    def test_an_unparseable_body_raises_public_dividend_error(self, monkeypatch):
        class _BadJson(_Resp):
            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr("httpx.get", lambda *a, **kw: _BadJson({}))
        with pytest.raises(PublicDividendError):
            fetch_public_dividends("SCHB")
