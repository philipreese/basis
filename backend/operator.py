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

No standalone entrypoint: the executor pipeline (`pixi run executor-nightly`)
runs this module's functions as part of its nightly pass. All results are
persisted through the same models the UI reads, so the web app shows the
operator's work on next open. This module places no orders — the order path
lives in executor.py (#32).
"""

import datetime
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

# Headless entrypoint: unlike the uvicorn app (main.py loads .env itself),
# nothing else populates the environment before this module reads it.
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import httpx
from sqlalchemy import select

from backend.catalyst_calendar import merge_catalysts
from backend.database import async_session_maker
from backend.dates import market_today
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
    in_flight_close_orders,
    run_exposure_safeguards,
    run_lifecycle_scan,
)
from backend.opportunity import scan_opportunities
from backend.regime import compute_regime
from backend.regime_variants import persist_regime_readings
from backend.states import POSITION_OPEN_STATUS

logger = logging.getLogger(__name__)

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# index_history ingestion (#62): symbols the V1/V2 regime variants need,
# how far back the first run reaches, and the incremental top-up window.
# SPY closes feed SMA200 and RV20 (#69); IWM/GLD/TLT feed the
# per-underlying telemetry and RV-rank pseudo-IVR for B09/B10/B22
# (#139, #135).
# VIX9D/HYG/LQD/RSP feed the observation-only engines V4-V6 (#251).
INDEX_SYMBOLS = ("VIX", "VIX3M", "SPY", "IWM", "GLD", "TLT", "VIX9D", "HYG", "LQD", "RSP", "AAPL")
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
    result = await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))
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
            pos.last_priced_at = datetime.datetime.now(datetime.UTC).isoformat()  # mark freshness (#280)
            updated += 1

    await session.commit()
    return updated


async def refresh_market_state(session, today: datetime.date | None = None) -> tuple[MarketStateModel | None, bool]:
    """Fetch live telemetry and recompute the regime.

    Returns (market state model, telemetry_is_live). On fetch failure the
    stored state is returned unchanged with telemetry_is_live=False.

    *today* is the run's market date (#540); defaults to market_today() for
    the standalone-operator entrypoint, which has no executor run to thread
    it from.
    """
    today = today or market_today()
    result = await session.execute(select(MarketStateModel).filter_by(id=1))
    state = result.scalar_one_or_none()

    telemetry = fetch_market_telemetry()
    if telemetry is None:
        return state, False

    if state is None:
        state = MarketStateModel(id=1)
        session.add(state)

    existing_ivrs = state.underlying_ivrs or {}
    # Seeded FOMC/CPI dates merge in additively (#131) — manual entries are
    # preserved, long-past ones pruned, and the merge is idempotent.
    existing_catalysts = merge_catalysts(state.catalyst_dates or [], today)
    winning_regime, scores = compute_regime(
        spy_price=telemetry["spy_price"],
        spy_sma20=telemetry["spy_sma20"],
        vix_close=telemetry["vix_close"],
        underlying_ivrs=existing_ivrs,
        spy_daily_return=telemetry["spy_daily_return"],
        catalyst_dates=existing_catalysts,
        today=today,
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
    # #602: a position already carrying a non-terminal CLOSE order (STAGED,
    # SUBMITTED, or PARTIAL) is being handled — re-paging the operator to
    # close it risks a duplicate exit. Split it out of the "needs a human
    # tonight" counts/priority while still SHOWING it (never silent), framed
    # as in-flight rather than actionable.
    p1_all = [r for r in lifecycle if r["priority"].startswith("P1")]
    p2_all = [r for r in lifecycle if r["priority"].startswith("P2")]
    p1 = [r for r in p1_all if not r.get("close_in_flight")]
    p2 = [r for r in p2_all if not r.get("close_in_flight")]
    p1_in_flight = [r for r in p1_all if r.get("close_in_flight")]
    p2_in_flight = [r for r in p2_all if r.get("close_in_flight")]
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
    for r in p1_in_flight + p2_in_flight:
        since = r.get("close_in_flight_since")
        status = f"submitted {since}" if since else "staged, awaiting the next submission attempt"
        lines.append(f"{r['priority']}: {r['underlying']} {r['strategy_type']} — close already in flight ({status})")

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
            # #560: HTTP header VALUES are ASCII-only — httpx raises
            # UnicodeEncodeError while constructing the request for any
            # non-ASCII str header, and it raises BEFORE the request ever
            # reaches the network. The executor's urgent-push title
            # ("⛔ basis executor alerts") is hardcoded with an emoji, so
            # every urgent push hit this: the except below swallowed the
            # client-side encode error and reported False — permanently
            # UNDELIVERED, even though nothing was ever attempted, let alone
            # rejected by ntfy. Encoding the title as UTF-8 bytes sidesteps
            # httpx's str-header ASCII check; ntfy's server reads UTF-8
            # header bytes directly (docs.ntfy.sh/publish/#message-title).
            headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": "chart_with_upwards_trend"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Failed to push ntfy digest: %s", exc)
        return False


def send_ntfy_with_retry(
    title: str, body: str, priority: str = "default", attempts: int = 3, backoff_seconds: float = 2.0
) -> bool:
    """send_ntfy with exponential backoff (#277, audit H2): the digest is the
    system's only nightly voice — one transient network blip must not silence
    it. A missing NTFY_TOPIC fails immediately (retrying can't configure it)."""
    if not os.getenv("NTFY_TOPIC"):
        return send_ntfy(title, body, priority)
    for attempt in range(attempts):
        if send_ntfy(title, body, priority):
            return True
        if attempt < attempts - 1:
            time.sleep(backoff_seconds * (2**attempt))
    return False


def _file_backed_sqlite_sync_url(database_url: str) -> str | None:
    """#472: DATABASE_URL.replace("sqlite+aiosqlite://", "sqlite://") silently
    no-ops for any non-sqlite URL (Postgres, etc.) — create_engine would then
    open THAT scheme with the alien connect_args below and fail unpredictably
    — and maps a bare ":memory:" URL to a brand-new, empty in-memory database
    distinct from the process's real one, so the write would silently vanish.
    Returns the rewritten sync URL, or None when the audit half must be
    skipped."""
    if not database_url.startswith("sqlite+aiosqlite:///"):
        return None
    path = database_url.removeprefix("sqlite+aiosqlite:///")
    if not path or path == ":memory:":
        return None
    return database_url.replace("sqlite+aiosqlite://", "sqlite://")


def alert_crash(title: str, body: str, priority: str = "urgent", event_type: str = "CRASH_ALERT") -> None:
    """Crash-path alert (#417): audit row FIRST, then ntfy with retry.

    Crash alerts were bare send_ntfy — if ntfy was unreachable (and the
    crash may BE a network problem), the operator learned nothing until the
    22:00 watchdog. The database usually survives a crash, so the durable
    record goes there; the audit row also makes the crash visible in the
    console's event feed regardless of what the phone received. Both halves
    swallow their own failures — an alert must never crash the crash path.

    event_type distinguishes a genuine unhandled exception (CRASH_ALERT,
    the default) from a known scheduler/config condition — Gateway never
    came up, IBC_START_SCRIPT missing, a nightly backup step failing — which
    callers should pass as SCHEDULER_ALERT (#472): every _urgent/alert_crash
    call used to land as CRASH_ALERT regardless, so the audit trail couldn't
    tell "the code crashed" from "the environment wasn't ready."
    """
    try:
        # A SYNC engine on purpose: crash paths run from both sync entry
        # points (gateway_lifecycle, fill_check) and async ones (flex_audit)
        # — asyncio.run() here would explode inside a running loop.
        import json

        from sqlalchemy import create_engine, text

        from backend.database import DATABASE_URL, _install_sqlite_pragmas

        sync_url = _file_backed_sqlite_sync_url(DATABASE_URL)
        if sync_url is None:
            logger.warning(
                "alert_crash: DATABASE_URL %r is not a file-backed sqlite database — skipping the audit row",
                DATABASE_URL,
            )
        else:
            # #472: under DB contention (plausibly the crash's own cause,
            # e.g. the executor's long-lived session still holding a write
            # lock) a zero-timeout connection fails immediately — exactly
            # when this audit row matters most. WAL + busy_timeout match the
            # production engine (backend.database, #271) so this throwaway
            # engine waits instead of losing the write.
            engine = create_engine(sync_url, connect_args={"timeout": 15})
            _install_sqlite_pragmas(engine)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO audit_events (run_at, book_id, event_type, actor, payload) "
                            "VALUES (:run_at, NULL, :event_type, 'system', :payload)"
                        ),
                        {
                            "run_at": datetime.datetime.now(datetime.UTC).isoformat(),
                            "event_type": event_type,
                            "payload": json.dumps({"title": title, "body": body}),
                        },
                    )
            finally:
                engine.dispose()
    except Exception as exc:  # pragma: no cover - double-fault path
        logger.warning("Crash audit row failed: %s", exc)
    send_ntfy_with_retry(title, body, priority)


async def run_evening_operation(session_maker=None) -> tuple[str, str, str]:
    """Execute the full evening pipeline; returns the composed digest."""
    session_maker = session_maker or async_session_maker
    today = market_today()  # #540: computed once for this run, not per-call
    async with session_maker() as session:
        repriced = await refresh_position_values(session)
        state, telemetry_live = await refresh_market_state(session, today)
        index_rows = await persist_index_history(session)
        logger.info("index_history: %d new row(s) persisted", index_rows)
        variant_readings = await persist_regime_readings(session)
        logger.info("regime readings: %s", variant_readings or "skipped (no market state)")

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
        # #602: don't let the nightly digest urgent-page an operator to close
        # a position the system already submitted or staged a close for.
        close_in_flight = await in_flight_close_orders(session, [p.id for p in open_positions])
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
                    "close_in_flight": pos.id in close_in_flight,
                    "close_in_flight_since": close_in_flight.get(pos.id),
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
