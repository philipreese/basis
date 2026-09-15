"""#1035: entries gate on the variance risk premium, not the RV rank.

The bug this replaces: `underlying_ivrs` has never held an implied-vol rank.
It was a hand-typed 25.0 until #992 and the RV20 percentile rank since — a
measure of how much the market has been MOVING. Every playbook's min_ivr
floor read it as "how expensive is the premium", which is close to the
opposite question: a quiet tape with normal implied vol is the best
short-premium environment there is, and the floors refused precisely that.

Observed 2026-09-15: rank 12, VIX 17.1, so roughly 7-11 vol points of real
edge — and 31 of 35 books stood down because the rank was "too low".
"""

import pytest

from backend.eligibility import check_entry_filters
from backend.models import MarketStateSchema, PlaybookDefinitionSchema
from backend.regime_variants import VRP_FULL_EDGE
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS


def market(*, vix: float, rv20: float, rank: float = 12.0, regime: str = "CALM_BULL") -> MarketStateSchema:
    return MarketStateSchema(
        current_regime=regime,
        spy_price=760.0,
        spy_sma20=766.0,
        vix_close=vix,
        spy_rv20=rv20,
        underlying_ivrs={"SPY": rank},
        spy_daily_return=-0.004,
        catalyst_dates=[],
    )


def playbook(pid: str) -> PlaybookDefinitionSchema:
    return PlaybookDefinitionSchema(**next(p for p in SEED_PLAYBOOKS if p["id"] == pid))


CONDOR = "spy_iron_condor_v1"
STRADDLE = "xsp_long_straddle_catalyst_v1"


class TestTheObservedNight:
    """The exact state that prompted this change."""

    def test_a_quiet_tape_with_normal_vol_now_trades(self):
        # rank 12 (quiet), VIX 17.1, RV20 8 -> VRP 9.1. Under the old floor
        # of 50 this was refused; it is the best setup a seller gets.
        assert check_entry_filters(playbook(CONDOR), market(vix=17.1, rv20=8.0)) is None

    def test_the_old_rank_floor_no_longer_refuses_it(self):
        reason = check_entry_filters(playbook(CONDOR), market(vix=17.1, rv20=8.0))
        assert reason is None or "realized-vol rank" not in reason


class TestTheGateStillRefusesRealRisk:
    def test_thin_premium_is_refused(self):
        # VIX 17 against RV20 16 is one vol point of edge -- not worth it.
        reason = check_entry_filters(playbook(CONDOR), market(vix=17.0, rv20=16.0))
        assert reason is not None and "VRP" in reason

    def test_negative_premium_is_refused(self):
        # The market is moving MORE than the options imply: selling here
        # pays you less than the realized risk.
        reason = check_entry_filters(playbook(CONDOR), market(vix=14.0, rv20=22.0))
        assert reason is not None and "VRP" in reason

    def test_the_floor_is_the_projects_own_constant(self):
        # Exactly at the floor passes; a hair under does not. RV20 15 keeps
        # VIX inside the condor's own [15-35] band so this isolates the VRP
        # boundary rather than tripping the older VIX-range filter.
        assert check_entry_filters(playbook(CONDOR), market(vix=15.0 + VRP_FULL_EDGE, rv20=15.0)) is None
        reason = check_entry_filters(playbook(CONDOR), market(vix=15.0 + VRP_FULL_EDGE - 0.1, rv20=15.0))
        assert reason is not None and "VRP" in reason


class TestFailClosed:
    def test_missing_rv20_holds_entries_rather_than_assuming_zero(self):
        # A market_state written before #1035 has spy_rv20 = 0.0. Treating
        # that as "RV20 is zero" would compute VRP = VIX and wave every
        # entry through on the richest-looking number in the system.
        reason = check_entry_filters(playbook(CONDOR), market(vix=17.1, rv20=0.0))
        assert reason is not None
        assert "no RV20" in reason

    def test_a_playbook_without_a_vrp_floor_is_unaffected(self):
        # Buyers set no min_vrp: paying up is their trade, not their risk.
        assert playbook(STRADDLE).entry_filters.min_vrp is None


class TestSeedIntent:
    @pytest.mark.parametrize(
        "pid",
        ["spy_iron_condor_v1", "spy_broken_wing_butterfly_v1", "spy_bull_put_spread_v1", "spy_bear_call_spread_v1"],
    )
    def test_every_credit_seller_gates_on_vrp(self, pid):
        assert playbook(pid).entry_filters.min_vrp == VRP_FULL_EDGE

    def test_no_playbook_still_floors_on_the_rank(self):
        # The floor is the bug. A non-zero one ANYWHERE means some playbook
        # can still stand down for "the market has been quiet", which is the
        # reason to trade, not the reason to abstain. Enumerated over the
        # whole seed rather than a hand-listed subset: the floor that
        # survived the first pass of this change (aapl_earnings_condor_v1,
        # reachable through B30) was exactly the one nobody thought to list.
        floored = {pb["id"]: pb["entry_filters"]["min_ivr"] for pb in SEED_PLAYBOOKS if pb["entry_filters"]["min_ivr"]}
        assert floored == {}

    def test_no_book_reintroduces_a_floor_by_override(self):
        # A lab book can override any entry filter (executor._book_playbooks),
        # so the seed being clean is only half the guarantee.
        offenders = {
            spec["id"]: (spec["config"].get("playbook_overrides") or {})["entry_filters.min_ivr"]
            for spec in LAB_BOOKS
            if (spec["config"].get("playbook_overrides") or {}).get("entry_filters.min_ivr")
        }
        assert offenders == {}

    def test_the_debit_ivr_ceiling_survives(self):
        # eligibility.py's IVR>70 suppression of naked long vol is a
        # SEPARATE and correct rule; this change must not remove it.
        from backend.eligibility import DEBIT_NAKED

        assert "LONG_STRADDLE" in DEBIT_NAKED


class TestTheUpgradePath:
    def test_a_row_written_before_the_column_existed_still_reads(self):
        """The nightly run must survive its own migration (#1035).

        `spy_rv20` is added to a live database by database.py's additive
        migration. A row written before it existed reads NULL unless the
        column carries a server default — and MarketStateSchema types the
        field `float`, so a NULL would raise inside to_schema() and take the
        whole run down on the first night after the upgrade. Fail-closed on
        0.0 is the intended behaviour; a crash is not.
        """
        from backend.models import MarketStateModel

        row = MarketStateModel(
            id=1,
            current_regime="CALM_BULL",
            spy_price=760.0,
            spy_sma20=750.0,
            vix_close=17.1,
            spy_rv20=None,
            underlying_ivrs={"SPY": 12.0},
            spy_daily_return=-0.004,
            catalyst_dates=[],
            regime_scores={},
        )
        assert row.to_schema().spy_rv20 == 0.0
        reason = check_entry_filters(playbook(CONDOR), row.to_schema())
        assert reason is not None and "no RV20" in reason

    def test_the_column_carries_a_server_default(self):
        from backend.models import MarketStateModel

        assert MarketStateModel.__table__.c.spy_rv20.server_default is not None
