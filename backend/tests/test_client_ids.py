"""The broker session and market-data fetches must never share a client id.

IBKR allows one API connection per client id. The executor's broker session
holds IBKR_CLIENT_ID for its whole run while market_data opens transient
fetch connections — sharing an id is Error 326 and a full telemetry
blackout, discovered on the first live executor-nightly run (#198).
"""

from backend.market_data import _data_client_id, _gateway_config


def test_default_ids_differ(monkeypatch):
    monkeypatch.delenv("IBKR_CLIENT_ID", raising=False)
    monkeypatch.delenv("IBKR_DATA_CLIENT_ID", raising=False)
    _, _, session_id = _gateway_config()
    assert _data_client_id() != session_id


def test_misconfigured_equal_ids_still_never_collide(monkeypatch):
    monkeypatch.setenv("IBKR_CLIENT_ID", "18")
    monkeypatch.setenv("IBKR_DATA_CLIENT_ID", "18")
    _, _, session_id = _gateway_config()
    assert _data_client_id() != session_id
