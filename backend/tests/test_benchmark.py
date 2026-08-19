"""The SPY buy-and-hold benchmark (#211): the null hypothesis in the digest.
Anchored on the first fill; silent until the experiment has actually started."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.benchmark import spy_benchmark_line
from backend.models import Base, FillModel, IndexHistoryModel


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _fill(exec_id: str, fill_time: str) -> FillModel:
    return FillModel(
        exec_id=exec_id,
        order_id="ord_1",
        book_id="B01",
        con_id=1,
        side="SELL",
        quantity=1.0,
        price=0.49,
        fill_time=fill_time,
    )


def _spy(date: str, close: float) -> IndexHistoryModel:
    return IndexHistoryModel(date=date, symbol="SPY", close=close)


@pytest.mark.asyncio
async def test_no_fills_means_no_benchmark(session):
    session.add(_spy("2026-08-19", 770.0))
    session.add(_spy("2026-08-20", 780.0))
    await session.commit()
    assert await spy_benchmark_line(session) is None


@pytest.mark.asyncio
async def test_too_little_history_means_no_benchmark(session):
    session.add(_fill("e1", "2026-08-19T18:50:00Z"))
    session.add(_spy("2026-08-19", 770.0))
    await session.commit()
    assert await spy_benchmark_line(session) is None


@pytest.mark.asyncio
async def test_window_starts_at_first_fill(session):
    session.add(_fill("e2", "2026-08-20T18:50:00Z"))
    session.add(_fill("e1", "2026-08-19T18:50:00Z"))  # earliest wins
    # A close BEFORE the first fill must not be the anchor.
    session.add(_spy("2026-08-01", 700.0))
    session.add(_spy("2026-08-19", 750.0))
    session.add(_spy("2026-09-19", 780.0))
    await session.commit()
    line = await spy_benchmark_line(session)
    # 10_000 * 780/750 = 10_400, +4.0%
    assert line == "Benchmark: $10K in SPY → $10,400 (+4.0%) since 2026-08-19 (price return, excl. dividends)"


@pytest.mark.asyncio
async def test_drawdown_renders_negative(session):
    session.add(_fill("e1", "2026-08-19T18:50:00Z"))
    session.add(_spy("2026-08-19", 800.0))
    session.add(_spy("2026-08-28", 720.0))
    await session.commit()
    line = await spy_benchmark_line(session)
    assert "$9,000 (-10.0%)" in line


@pytest.mark.asyncio
async def test_other_symbols_are_ignored(session):
    session.add(_fill("e1", "2026-08-19T18:50:00Z"))
    session.add(IndexHistoryModel(date="2026-08-19", symbol="VIX", close=15.0))
    session.add(IndexHistoryModel(date="2026-08-20", symbol="VIX", close=16.0))
    await session.commit()
    assert await spy_benchmark_line(session) is None
