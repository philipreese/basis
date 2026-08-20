"""Tests for the compose functions extracted from the fat routes (#179).

These previously lived inside main.py route bodies, testable only through
HTTP. The compose interface is the test surface now — no app, no DB.
"""

from backend.observation import compose_observation
from backend.performance import compose_diagnostics
from backend.tests.test_experiment_matrix import _position
from backend.tests.test_opportunity import _make_market_state, _make_portfolio_config


class TestComposeObservation:
    def test_quiet_portfolio_composes_all_sections(self):
        pos = _position()
        result = compose_observation(_make_portfolio_config(), [pos], _make_market_state())
        (scanned,) = result["scanned_positions"]
        assert scanned["position_id"] == pos.id
        assert scanned["priority"]  # lifecycle scan ran
        assert "net_delta" in result["greeks"]
        assert result["market_state"].current_regime == "CALM_BULL"
        assert not any(w["type"].startswith("GREEK_LIMIT") for w in result["safeguards"])

    def test_greek_limit_breach_appends_critical_warning(self):
        config = _make_portfolio_config()
        config.portfolio_greek_limits.max_net_delta = 0.01
        result = compose_observation(config, [_position()], _make_market_state())
        (warning,) = [w for w in result["safeguards"] if w["type"] == "GREEK_LIMIT_DELTA"]
        assert warning["severity"] == "CRITICAL"

    def test_closed_positions_are_not_scanned_but_count_toward_greeks(self):
        closed = _position(pos_id="closed")
        closed = closed.model_copy(update={"status": "CLOSED"})
        result = compose_observation(_make_portfolio_config(), [closed], _make_market_state())
        assert result["scanned_positions"] == []


class TestComposeDiagnostics:
    def test_empty_inputs_yield_honest_note(self):
        result = compose_diagnostics([], {}, [], generated_at="2026-08-18T22:00:00+00:00")
        assert result.playbook_metrics == []
        assert result.benchmarks.spy_cagr is None
        assert "No benchmark data yet" in result.benchmarks.note
