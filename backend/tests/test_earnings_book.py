"""Tests for the AAPL earnings-crush arm B30 and scoped catalysts (#317).

The load-bearing invariant: "EARNINGS:AAPL:date" entries are visible ONLY
to AAPL's own playbooks — market regime engines and every other
underlying's catalyst filters are blind to them, so one stock's earnings
never blackouts the index books.
"""

import datetime

from backend.assignment_defense import entry_ex_div_block
from backend.eligibility import (
    check_entry_filters,
    has_catalyst_within_14dte,
    has_scoped_catalyst_within_14dte,
    relevant_catalysts,
)
from backend.market_data import ETF_SYMBOLS
from backend.models import MarketStateSchema, PlaybookDefinitionSchema
from backend.operator import INDEX_SYMBOLS
from backend.regime import catalyst_scope, classify_catalysts
from backend.regime_variants import catalysts_within_trading_days
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS

TODAY = datetime.date(2026, 10, 22)
AAPL_EARNINGS = "EARNINGS:AAPL:2026-10-29"


def _playbook(pb_id: str) -> PlaybookDefinitionSchema:
    raw = next(pb for pb in SEED_PLAYBOOKS if pb["id"] == pb_id)
    return PlaybookDefinitionSchema(**raw)


def _market_state(**overrides) -> MarketStateSchema:
    defaults: dict = {
        "current_regime": "CALM_BULL",
        "spy_price": 760.0,
        "spy_sma20": 750.0,
        "vix_close": 14.5,
        "underlying_ivrs": {"SPY": 60.0, "AAPL": 60.0},
        "spy_daily_return": 0.004,
        "catalyst_dates": [AAPL_EARNINGS],
        "regime_scores": {},
        "underlying_prices": {"AAPL": 230.0},
        "underlying_sma20": {"AAPL": 228.0},
    }
    defaults.update(overrides)
    return MarketStateSchema(**defaults)


class TestCatalystScope:
    def test_scoped_and_unscoped_forms(self):
        assert catalyst_scope("EARNINGS:AAPL:2026-10-29") == "AAPL"
        assert catalyst_scope("earnings:aapl:2026-10-29") == "AAPL"  # case-insensitive
        assert catalyst_scope("EARNINGS:BRK.B:2026-11-01") == "BRK.B"
        assert catalyst_scope("FOMC:2026-09-16") is None
        assert catalyst_scope("EARNINGS:2026-08-20") is None  # legacy unscoped earnings stay global
        assert catalyst_scope("2026-09-16") is None
        assert catalyst_scope("watch jackson hole") is None

    def test_relevance_filtering(self):
        entries = ["FOMC:2026-10-28", AAPL_EARNINGS]
        assert relevant_catalysts(entries, "SPY") == ["FOMC:2026-10-28"]
        assert relevant_catalysts(entries, "AAPL") == entries
        assert has_catalyst_within_14dte([AAPL_EARNINGS], TODAY, underlying="SPY") is False
        assert has_catalyst_within_14dte([AAPL_EARNINGS], TODAY, underlying="AAPL") is True
        assert has_scoped_catalyst_within_14dte(["FOMC:2026-10-28"], "AAPL", TODAY) is False
        assert has_scoped_catalyst_within_14dte([AAPL_EARNINGS], "AAPL", TODAY) is True


class TestMarketEnginesAreBlind:
    def test_v0_catalyst_dimension_ignores_scoped_entries(self):
        assert classify_catalysts([AAPL_EARNINGS], TODAY) == "CATALYST_NONE"
        assert classify_catalysts([AAPL_EARNINGS, "FOMC:2026-10-28"], TODAY) == "CATALYST_MAJOR"

    def test_v3_trading_day_window_ignores_scoped_entries(self):
        assert catalysts_within_trading_days([AAPL_EARNINGS], TODAY, 5) == (False, False)
        # Legacy unscoped earnings entries still read as global MINOR.
        assert catalysts_within_trading_days(["EARNINGS:2026-10-27"], TODAY, 5) == (False, True)


class TestEntryFilters:
    def test_spy_income_playbook_not_blacked_out_by_aapl_earnings(self):
        # spy_iron_condor blocks entries around events — an AAPL-scoped
        # entry must NOT trip it.
        pb = _playbook("spy_iron_condor_v1")
        state = _market_state(vix_close=20.0)
        assert check_entry_filters(pb, state, today=TODAY) is None

    def test_earnings_condor_requires_the_scoped_event(self):
        pb = _playbook("aapl_earnings_condor_v1")
        with_event = check_entry_filters(pb, _market_state(), today=TODAY)
        assert with_event is None
        # A market-wide FOMC date is not an AAPL earnings play.
        without = check_entry_filters(pb, _market_state(catalyst_dates=["FOMC:2026-10-28"]), today=TODAY)
        assert without is not None and "AAPL-scoped" in without


class TestB30Seed:
    def test_b30_is_rv_gated_single_name_with_documented_confound(self):
        b30 = next(spec for spec in LAB_BOOKS if spec["id"] == "B30")
        cfg = b30["config"]
        assert cfg["underlying"] == "AAPL"
        assert cfg["ignore_regime"] is True
        assert cfg["playbook_ids"] == ["aapl_earnings_condor_v1"]
        assert cfg["envelope"]["max_loss_pct_per_trade"] == 4.5
        # Disabled globally; enabled only through this book's whitelist.
        assert _playbook("aapl_earnings_condor_v1").enabled is False

    def test_aapl_telemetry_is_ingested_as_a_stock(self):
        assert "AAPL" in INDEX_SYMBOLS
        assert "AAPL" in ETF_SYMBOLS

    def test_spec_expiry_snaps_after_the_aapl_event_not_the_nearer_fomc(self):
        from backend.opportunity import generate_trade_spec
        from backend.seeds import SEED_PORTFOLIO_CONFIG

        # Enabled the way B30's whitelist override does at scan time.
        pb = _playbook("aapl_earnings_condor_v1").model_copy(update={"enabled": True})
        state = _market_state(catalyst_dates=["FOMC:2026-10-28", AAPL_EARNINGS])
        from backend.models import PortfolioConfigSchema

        config = PortfolioConfigSchema(**SEED_PORTFOLIO_CONFIG)
        result = generate_trade_spec(pb, state, positions=[], portfolio_config=config, contracts=1, today=TODAY)
        assert result.spec is not None, [b.reason for b in result.hard_blocks]
        exp = datetime.date.fromisoformat(result.spec.legs[0].expiration_date)
        # Crush snap (+3, not +14): after the 10-29 event, BEFORE the 11-09
        # ex-div — a +14 snap would span the ex-date and the short call would
        # be EX_DIV_ASSIGNMENT-blocked every single quarter.
        assert exp == datetime.date(2026, 11, 6)
        # Strikes land on AAPL's $2.5 grid, not the $1 default.
        assert all((leg.strike * 10) % 25 == 0 for leg in result.spec.legs)

    def test_time_exit_lands_after_the_event_for_every_report_weekday(self):
        # Audit II (#349): the +3 buffer put Mon/Tue reports on the SAME-week
        # Friday, so the old 7-DTE exit closed the condor ~4 days BEFORE the
        # event — buy elevated IV, exit before the crush, systematic loser.
        # With +6 and a 5-DTE exit, the earliest date the exit can fire
        # (expiry − 5) is strictly after the event for all five weekdays.
        from backend.opportunity import _target_expiration

        pb = _playbook("aapl_earnings_condor_v1")
        exit_dte = pb.exit_rules.mandatory_exit_dte
        assert exit_dte == 5
        for offset in range(5):  # Mon 2026-10-26 … Fri 2026-10-30
            event = datetime.date(2026, 10, 26) + datetime.timedelta(days=offset)
            exp, _dte = _target_expiration(
                today=event - datetime.timedelta(days=7),
                target_dte=14,
                require_after_catalyst=True,
                catalyst_dates=[f"EARNINGS:AAPL:{event.isoformat()}"],
                event_buffer_days=6,
            )
            earliest_exit = exp - datetime.timedelta(days=exit_dte)
            assert earliest_exit > event, f"{event:%A} report: exit {earliest_exit} not after event"
            assert exp.weekday() == 4  # still a Friday expiry

    def test_aapl_short_call_spanning_ex_div_is_blocked(self):
        # AAPL pays quarterly — the ex-div defense (No-Stock Mandate
        # defense-in-depth) must know its calendar.
        today = datetime.date(2026, 11, 2)
        assert (
            entry_ex_div_block("AAPL", has_short_call=True, today=today, expiration=today + datetime.timedelta(days=14))
            is not None
        )
