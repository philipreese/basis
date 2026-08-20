"""Tests for per-underlying telemetry and the B09/B10 books (#139).

The Layer C scan used spy_price for every strike derivation — right for
SPY/XSP, wrong by 2–3× for IWM/GLD. These tests pin the telemetry-proxy
lookups, the RV-rank pseudo-IVR, the index_history-backed snapshot, the
never-trade-blind suppression, and the per-book underlying rewrite.
"""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.book_gates import resolve_book_config
from backend.executor import _book_playbooks
from backend.models import Base, IndexHistoryModel
from backend.opportunity import (
    generate_trade_spec,
    scan_opportunities,
)
from backend.regime_variants import RV_RANK_MIN_CLOSES, rv_rank, underlying_telemetry
from backend.telemetry import telemetry_key
from backend.telemetry import underlying_price as _underlying_price
from backend.telemetry import underlying_sma20 as _underlying_sma20
from backend.tests.test_opportunity import _make_market_state, _make_playbook, _make_portfolio_config

TODAY = datetime.date(2026, 8, 18)


def _iwm_state(price: float = 230.0, sma20: float = 225.0, ivr: float = 55.0):
    return _make_market_state().model_copy(
        update={
            "underlying_prices": {"IWM": price},
            "underlying_sma20": {"IWM": sma20},
            "underlying_ivrs": {"SPY": 25.0, "IWM": ivr},
        }
    )


def _iwm_playbook():
    pb = _make_playbook(pb_id="iwm_bcs", strategy="BULL_CALL_SPREAD", min_ivr=0.0, vix_min=10.0)
    return pb.model_copy(update={"underlying_ticker": "IWM"})


class TestTelemetryProxy:
    def test_xsp_proxies_to_spy(self):
        assert telemetry_key("XSP") == "SPY"
        state = _make_market_state(spy_price=758.0)
        assert _underlying_price(state, "XSP") == 758.0
        assert _underlying_sma20(state, "XSP") == state.spy_sma20

    def test_non_spy_ticker_reads_the_per_underlying_dicts(self):
        state = _iwm_state()
        assert _underlying_price(state, "IWM") == 230.0
        assert _underlying_sma20(state, "IWM") == 225.0

    def test_missing_telemetry_is_none_never_spy(self):
        state = _make_market_state(spy_price=758.0)
        assert _underlying_price(state, "GLD") is None


class TestRvRank:
    def test_short_history_is_none(self):
        assert rv_rank([100.0 + i * 0.1 for i in range(RV_RANK_MIN_CLOSES - 1)]) is None

    def test_vol_spike_at_the_end_ranks_high(self):
        closes = [100.0] * 80 + [100.0 + (5.0 if i % 2 else -5.0) for i in range(21)]
        assert rv_rank(closes) == 100.0

    def test_calm_tail_after_wild_start_ranks_low(self):
        closes = [100.0 + (5.0 if i % 2 else -5.0) for i in range(60)] + [100.0] * 41
        rank = rv_rank(closes)
        assert rank is not None and rank < 50.0


@pytest_asyncio.fixture
async def session_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'mu.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _dates(n: int) -> list[str]:
    start = datetime.date(2026, 1, 1)
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


class TestUnderlyingTelemetry:
    @pytest.mark.asyncio
    async def test_full_history_yields_price_sma_and_pseudo_ivr(self, session_maker):
        closes = [230.0 + (i % 7) * 0.5 for i in range(80)]
        async with session_maker() as session:
            for date, close in zip(_dates(80), closes, strict=True):
                session.add(IndexHistoryModel(date=date, symbol="IWM", close=close))
            await session.commit()
            prices, smas, ivrs = await underlying_telemetry(session, ("IWM",))
        assert prices["IWM"] == closes[-1]
        assert smas["IWM"] == round(sum(closes[-20:]) / 20, 4)
        assert 0.0 <= ivrs["IWM"] <= 100.0

    @pytest.mark.asyncio
    async def test_short_history_is_absent_not_guessed(self, session_maker):
        async with session_maker() as session:
            for date in _dates(10):
                session.add(IndexHistoryModel(date=date, symbol="GLD", close=310.0))
            await session.commit()
            prices, smas, ivrs = await underlying_telemetry(session, ("GLD",))
        assert prices == {} and smas == {} and ivrs == {}


class TestScanUsesUnderlyingPrice:
    def test_iwm_strikes_derive_from_iwm_price(self):
        result = scan_opportunities([_iwm_playbook()], _iwm_state(), [], _make_portfolio_config(), today=TODAY)
        (card,) = result.candidates
        assert card.eligible
        assert card.strike_params is not None and card.strike_params.current_price == 230.0
        assert "IWM @$230.00" in card.strike_params.derivation_note

    def test_missing_telemetry_suppresses_instead_of_deriving_off_spy(self):
        state = _iwm_state().model_copy(update={"underlying_prices": {}})
        result = scan_opportunities([_iwm_playbook()], state, [], _make_portfolio_config(), today=TODAY)
        (card,) = result.candidates
        assert not card.eligible
        assert "TELEMETRY" in (card.suppressed_reason or "")

    def test_spec_generation_hard_blocks_without_telemetry(self):
        state = _iwm_state().model_copy(update={"underlying_prices": {}})
        result = generate_trade_spec(_iwm_playbook(), state, [], _make_portfolio_config(), today=TODAY)
        assert result.spec is None
        assert "UNDERLYING_TELEMETRY" in [b.check for b in result.hard_blocks]


class TestBookUnderlyingRewrite:
    def test_book_underlying_becomes_the_playbook_ticker(self):
        pb = _make_playbook(pb_id="ic", strategy="IRON_CONDOR")
        for underlying in ("XSP", "IWM"):
            (adjusted,) = _book_playbooks([pb], resolve_book_config({"underlying": underlying}))
            assert adjusted.underlying_ticker == underlying
        (untouched,) = _book_playbooks([pb], resolve_book_config({}))
        assert untouched.underlying_ticker == "SPY"
