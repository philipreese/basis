"""Operator — the nightly evening pipeline, run headless (ADR-0006, Operator level).

Mirrors the manual evening ritual in one unattended pass:

1. Refresh open-position values from live option quotes (best effort).
2. Fetch live SPY/VIX telemetry and recompute the regime (falls back to the
   stored market state when IB Gateway is unavailable — same graceful degradation
   as the API layer).
3. Layer A: lifecycle scan, portfolio Greeks, exposure safeguards.
4. Layer C: opportunity scan against active playbooks.
5. Compose a digest and push it via ntfy (NTFY_TOPIC in .env; skipped with a
   log line when unset).

Run with `pixi run operator`; scheduled nightly by
scripts/register-operator-task.ps1. All results are persisted through the
same models the UI reads, so the web app shows the operator's work on next
open. No orders are placed — execution arrives with the Executor level (#32).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Headless entrypoint: unlike the uvicorn app (main.py loads .env itself),
# nothing else populates the environment before this module reads it.
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import httpx
from sqlalchemy import select

from backend.database import async_session_maker, init_db
from backend.market_data import (
    fetch_index_daily_closes,
    fetch_market_telemetry,
    fetch_options_latest_quotes,
    format_occ_symbol,
)
from backend.models import (
    IndexHistoryModel,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
)
from backend.observation import (
    aggregate_portfolio_greeks,
    run_exposure_safeguards,
    run_lifecycle_scan,
)
from backend.opportunity import scan_opportunities
from backend.regime import compute_regime

logger = logging.getLogger(__name__)

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# index_history ingestion (#62): symbols the V1/V2 regime variants need,
# how far back the first run reaches, and the incremental top-up window.
INDEX_SYMBOLS = ("VIX", "VIX3M")
INDEX_BACKFILL_DAYS = 365
INDEX_TOPUP_DAYS = 10


async def persist_index_history(session) -> int:
    """Persist daily VIX/VIX3M closes into index_history.

    Backfills ~a year per symbol on first run, then tops up the last few
    trading days. Returns the number of new rows written; a symbol whose
    fetch fails is skipped (0 new rows) — the nightly cadence self-heals
    gaps on the next run via the top-up window.
    """
    written = 0
    for symbol in INDEX_SYMBOLS:
        existing = set((await session.execute(select(IndexHistoryModel.date).filter_by(symbol=symbol))).scalars().all())
        days = INDEX_TOPUP_DAYS if existing else INDEX_BACKFILL_DAYS
        rows = fetch_index_daily_closes(symbol, days)
        if rows is None:
            continue
        for date, close in rows:
            if date in existing:
                continue
            session.add(IndexHistoryModel(date=date, symbol=symbol, close=close))
            written += 1
    await session.commit()
    return written


async def refresh_position_values(session) -> int:
    """Update current_value_per_share for open positions from live quotes.

    Returns the number of positions repriced (0 when quotes are unavailable —
    the scan then runs on stored values, which the digest flags).
    """
    result = await session.execute(select(PositionModel).filter_by(status="OPEN"))
    open_positions = result.scalars().all()
    if not open_positions:
        return 0

    occ_symbols = [
        format_occ_symbol(
            underlying=pos.underlying,
            expiration=leg["expiration"],
            option_type=leg["option_type"],
            strike=leg["strike"],
        )
        for pos in open_positions
        for leg in pos.legs
    ]
    quotes = fetch_options_latest_quotes(occ_symbols)
    if not quotes:
        return 0

    updated = 0
    for pos in open_positions:
        long_val = 0.0
        short_val = 0.0
        all_legs_priced = True
        for leg in pos.legs:
            occ_sym = format_occ_symbol(
                underlying=pos.underlying,
                expiration=leg["expiration"],
                option_type=leg["option_type"],
                strike=leg["strike"],
            )
            if occ_sym not in quotes:
                all_legs_priced = False
                break
            if leg["direction"] == "LONG":
                long_val += quotes[occ_sym]
            else:
                short_val += quotes[occ_sym]
        if all_legs_priced:
            new_val = long_val - short_val if pos.premium_direction == "DEBIT" else short_val - long_val
            pos.current_value_per_share = round(new_val, 2)
            updated += 1

    await session.commit()
    return updated


async def refresh_market_state(session) -> tuple[MarketStateModel | None, bool]:
    """Fetch live telemetry and recompute the regime.

    Returns (market state model, telemetry_is_live). On fetch failure the
    stored state is returned unchanged with telemetry_is_live=False.
    """
    result = await session.execute(select(MarketStateModel).filter_by(id=1))
    state = result.scalar_one_or_none()

    telemetry = fetch_market_telemetry()
    if telemetry is None:
        return state, False

    if state is None:
        state = MarketStateModel(id=1)
        session.add(state)

    existing_ivrs = state.underlying_ivrs or {}
    existing_catalysts = state.catalyst_dates or []
    winning_regime, scores = compute_regime(
        spy_price=telemetry["spy_price"],
        spy_sma20=telemetry["spy_sma20"],
        vix_close=telemetry["vix_close"],
        underlying_ivrs=existing_ivrs,
        spy_daily_return=telemetry["spy_daily_return"],
        catalyst_dates=existing_catalysts,
    )
    state.current_regime = winning_regime
    state.spy_price = telemetry["spy_price"]
    state.spy_sma20 = telemetry["spy_sma20"]
    state.vix_close = telemetry["vix_close"]
    state.spy_daily_return = telemetry["spy_daily_return"]
    state.underlying_ivrs = existing_ivrs
    state.catalyst_dates = existing_catalysts
    state.regime_scores = {k: float(v) for k, v in scores.items()}
    await session.commit()
    return state, True


def compose_digest(
    *,
    regime: str,
    spy_price: float,
    vix_close: float,
    telemetry_live: bool,
    positions_repriced: int,
    lifecycle: list[dict],
    safeguards: list[dict],
    scan_result,
) -> tuple[str, str, str]:
    """Build (title, body, ntfy_priority) for the evening digest."""
    p1 = [r for r in lifecycle if r["priority"].startswith("P1")]
    p2 = [r for r in lifecycle if r["priority"].startswith("P2")]
    eligible = [c for c in scan_result.candidates if c.eligible] if scan_result else []

    title_bits: list[str] = []
    if p1:
        title_bits.append(f"{len(p1)} CLOSE NOW")
    if p2:
        title_bits.append(f"{len(p2)} review")
    if eligible:
        title_bits.append(f"{len(eligible)} candidate{'s' if len(eligible) != 1 else ''}")
    if not title_bits:
        title_bits.append("all quiet")
    title = "basis evening: " + ", ".join(title_bits)

    lines: list[str] = [f"Regime {regime} | SPY {spy_price:.2f} | VIX {vix_close:.1f}"]
    if not telemetry_live:
        lines.append("⚠ Live telemetry unavailable — scan ran on stored data")
    if positions_repriced:
        lines.append(f"{positions_repriced} position(s) repriced from live quotes")

    for r in p1 + p2:
        lines.append(f"{r['priority']}: {r['underlying']} {r['strategy_type']} — {r['reason']}")

    for w in safeguards:
        lines.append(f"⚠ {w['type']}: {w['message']}")

    if scan_result is not None and scan_result.block_reason:
        lines.append(f"Scan blocked: {scan_result.block_reason}")
    for c in eligible:
        lines.append(f"Candidate: {c.playbook.name} — open the app for the full spec")
    if not eligible and scan_result is not None and not scan_result.block_reason:
        lines.append("No eligible playbooks tonight")

    priority = "high" if p1 or safeguards else "default"
    return title, "\n".join(lines), priority


def send_ntfy(title: str, body: str, priority: str = "default") -> bool:
    """Push the digest to the private ntfy topic. Returns False when skipped/failed."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC not set — digest not pushed:\n%s\n%s", title, body)
        return False
    try:
        resp = httpx.post(
            f"{NTFY_SERVER}/{topic}",
            content=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "chart_with_upwards_trend"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Failed to push ntfy digest: %s", exc)
        return False


async def run_evening_operation(session_maker=None) -> tuple[str, str, str]:
    """Execute the full evening pipeline; returns the composed digest."""
    session_maker = session_maker or async_session_maker
    async with session_maker() as session:
        repriced = await refresh_position_values(session)
        state, telemetry_live = await refresh_market_state(session)
        index_rows = await persist_index_history(session)
        logger.info("index_history: %d new row(s) persisted", index_rows)

        config_model = (await session.execute(select(PortfolioConfigModel).filter_by(id=1))).scalar_one_or_none()
        if config_model is None:
            raise RuntimeError("Portfolio config missing — has the database been initialized?")
        config = config_model.to_schema()

        positions = [p.to_schema() for p in (await session.execute(select(PositionModel))).scalars().all()]
        playbooks = [pb.to_schema() for pb in (await session.execute(select(PlaybookDefinitionModel))).scalars().all()]

        if state is None:
            raise RuntimeError("No market state available (no stored state and live fetch failed)")
        state_schema = state.to_schema()

        open_positions = [p for p in positions if p.status == "OPEN"]
        lifecycle = []
        for pos in open_positions:
            scan_res = run_lifecycle_scan(
                pos,
                current_regime=state_schema.current_regime,
                spy_price=state_schema.spy_price,
                catalyst_dates=state_schema.catalyst_dates,
            )
            lifecycle.append(
                {
                    "position_id": pos.id,
                    "underlying": pos.underlying,
                    "strategy_type": pos.strategy_type,
                    "priority": scan_res["priority"],
                    "reason": scan_res["reason"],
                }
            )

        safeguards = run_exposure_safeguards(positions, config)
        greeks = aggregate_portfolio_greeks(positions)
        limits = config.portfolio_greek_limits
        for greek, value_key, limit in [
            ("DELTA", "net_delta", limits.max_net_delta),
            ("VEGA", "net_vega", limits.max_net_vega),
            ("GAMMA", "net_gamma", limits.max_net_gamma),
        ]:
            if abs(greeks[value_key]) > limit:
                safeguards.append(
                    {
                        "type": f"GREEK_LIMIT_{greek}",
                        "severity": "CRITICAL",
                        "message": f"Portfolio net {greek.lower()} {greeks[value_key]:.1f} exceeds limit {limit}.",
                    }
                )

        scan_result = scan_opportunities(
            playbooks=playbooks,
            market_state=state_schema,
            positions=positions,
            portfolio_config=config,
        )

    title, body, priority = compose_digest(
        regime=state_schema.current_regime,
        spy_price=state_schema.spy_price,
        vix_close=state_schema.vix_close,
        telemetry_live=telemetry_live,
        positions_repriced=repriced,
        lifecycle=lifecycle,
        safeguards=safeguards,
        scan_result=scan_result,
    )
    return title, body, priority


async def main() -> None:
    # Windows consoles default to cp1252, which can't print the digest's
    # warning glyphs — degrade unprintable characters instead of crashing.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    # Alembic's fileConfig (alembic.ini) resets the root logger to WARN during
    # init_db — restore INFO so the run's outcome is visible in task logs.
    logging.getLogger().setLevel(logging.INFO)
    title, body, priority = await run_evening_operation()
    pushed = send_ntfy(title, body, priority)
    logger.info("Evening operation complete. Digest %s.", "pushed" if pushed else "NOT pushed (see warnings above)")
    print(f"\n{title}\n{body}")


if __name__ == "__main__":
    asyncio.run(main())
