"""Tests for the long-volatility event-catalyst arm B35 (#993).

The load-bearing invariant: B35 is the only book intentionally reaching a
long-vol strategy through the enforced EVENT_CATALYST table. Existing B12 and
B32 ignore_regime exceptions can behave independently of that table.
"""

import datetime

from backend.eligibility import REGIME_ALLOWED_STRATEGIES, has_catalyst_within_14dte
from backend.eligibility import check_entry_filters as _check_entry_filters
from backend.eligibility import check_regime_gate as _check_regime_gate
from backend.models import MarketStateSchema, PlaybookDefinitionSchema
from backend.opportunity import _target_expiration
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS

TODAY = datetime.date(2026, 9, 9)
PLAYBOOK_ID = "xsp_long_straddle_catalyst_v1"


def _playbook(enabled: bool = True) -> PlaybookDefinitionSchema:
    raw = next(pb for pb in SEED_PLAYBOOKS if pb["id"] == PLAYBOOK_ID)
    pb = PlaybookDefinitionSchema(**raw)
    return pb.model_copy(update={"enabled": enabled}) if enabled else pb


def _market_state(**overrides) -> MarketStateSchema:
    defaults: dict = {
        "current_regime": "EVENT_CATALYST",
        "spy_price": 760.0,
        "spy_sma20": 750.0,
        "vix_close": 22.0,
        "underlying_ivrs": {"SPY": 55.0, "XSP": 55.0},
        "spy_daily_return": 0.001,
        "catalyst_dates": ["FOMC:2026-09-16"],
        "regime_scores": {},
        "underlying_prices": {"XSP": 76.0},
        "underlying_sma20": {"XSP": 75.0},
    }
    defaults.update(overrides)
    return MarketStateSchema(**defaults)


class TestB35Seed:
    def test_b35_whitelists_the_long_vol_playbook_on_xsp(self):
        b35 = next(spec for spec in LAB_BOOKS if spec["id"] == "B35")
        cfg = b35["config"]
        assert cfg["underlying"] == "XSP"
        assert cfg["engine_variant"] == "V0"
        assert cfg["playbook_ids"] == [PLAYBOOK_ID]
        assert cfg["playbook_overrides"] == {"enabled": True}
        assert cfg["envelope"]["max_positions"] == 2
        assert b35["initial_control"]["state"] == "HALT_ENTRIES"
        # Reachable through the real regime gate — no control-flag escape
        # hatch, unlike B32's LONG_PUT (no allowed regime, ignore_regime).
        assert "ignore_regime" not in cfg

    def test_playbook_ships_disabled_globally(self):
        # Disabled by default; B35's whitelist + override only becomes
        # effective after an operator resumes B35's initially halted control.
        assert _playbook(enabled=False).enabled is False


class TestRegimeReachability:
    def test_long_straddle_is_allowed_only_under_event_catalyst(self):
        assert "LONG_STRADDLE" in REGIME_ALLOWED_STRATEGIES["EVENT_CATALYST"]
        for regime in ("CALM_BULL", "HIGH_VOL_NEUTRAL", "TRENDING_BEAR"):
            assert "LONG_STRADDLE" not in REGIME_ALLOWED_STRATEGIES[regime]

    def test_regime_gate_passes_under_event_catalyst(self):
        pb = _playbook()
        assert _check_regime_gate(pb, _market_state(current_regime="EVENT_CATALYST")) is None

    def test_regime_gate_blocks_outside_event_catalyst(self):
        pb = _playbook()
        for regime in ("CALM_BULL", "HIGH_VOL_NEUTRAL", "TRENDING_BEAR"):
            reason = _check_regime_gate(pb, _market_state(current_regime=regime))
            assert reason is not None and "REGIME GATE" in reason


class TestCatalystBoundary:
    def test_entry_passes_with_catalyst_exactly_14_days_out(self):
        pb = _playbook()
        state = _market_state(catalyst_dates=["2026-09-23"])  # TODAY + 14
        assert has_catalyst_within_14dte(state.catalyst_dates, TODAY) is True
        assert _check_entry_filters(pb, state, today=TODAY) is None

    def test_entry_blocked_with_catalyst_15_days_out(self):
        pb = _playbook()
        state = _market_state(catalyst_dates=["2026-09-24"])  # TODAY + 15
        assert has_catalyst_within_14dte(state.catalyst_dates, TODAY) is False
        reason = _check_entry_filters(pb, state, today=TODAY)
        assert reason is not None and "catalyst" in reason.lower()

    def test_entry_blocked_with_no_catalyst(self):
        pb = _playbook()
        reason = _check_entry_filters(pb, _market_state(catalyst_dates=[]), today=TODAY)
        assert reason is not None and "catalyst" in reason.lower()


class TestExpiryAndExitTiming:
    def test_expiry_snaps_at_least_14_days_past_the_event(self):
        event = datetime.date(2026, 9, 16)
        exp, actual_dte = _target_expiration(
            today=TODAY,
            target_dte=38,
            require_after_catalyst=True,
            catalyst_dates=[f"FOMC:{event.isoformat()}"],
            event_buffer_days=14,
        )
        assert exp >= event + datetime.timedelta(days=14)
        assert actual_dte == (exp - TODAY).days

    def test_mandatory_exit_lands_after_the_event_not_before(self):
        # mandatory_exit_dte=10 against a 14-day buffer: the earliest the
        # exit can fire (expiry - 10) is still >= event + 4 days, so the
        # arm never closes before the crush it exists to observe.
        pb = _playbook()
        exit_dte = pb.exit_rules.mandatory_exit_dte
        assert exit_dte == 10
        for offset in range(7):  # entry any day of the 14-day window
            entry_today = datetime.date(2026, 9, 2) + datetime.timedelta(days=offset)
            event = datetime.date(2026, 9, 16)
            exp, _dte = _target_expiration(
                today=entry_today,
                target_dte=38,
                require_after_catalyst=True,
                catalyst_dates=[f"FOMC:{event.isoformat()}"],
                event_buffer_days=14,
            )
            earliest_exit = exp - datetime.timedelta(days=exit_dte)
            assert earliest_exit > event, f"entry {entry_today}: exit {earliest_exit} not after event {event}"
