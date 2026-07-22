"""
session_scan.py — Automatic Evening Scan orchestration

Chains the existing Layer B live fetch, position price refresh, Layer A
lifecycle scan, and Layer C opportunity scan into a single call, gated to
run once per calendar day unless forced. Degrades gracefully (never raises)
when Alpaca credentials are absent or a live call fails, matching the
convention already used by backend/market_data.py.

Contains no FastAPI dependency — callable directly with an AsyncSession,
so it is unit-testable without any HTTP/ASGI transport involved.
"""

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    PortfolioConfigModel,
    PositionModel,
    PlaybookDefinitionModel,
    MarketStateModel,
    MarketStateSchema,
    SessionScanStateModel,
    SessionScanStateSchema,
    EveningScanResponse,
)
from backend.market_data import (
    fetch_market_telemetry,
    format_occ_symbol,
    fetch_options_latest_quotes,
    is_configured,
)
from backend.regime import compute_regime
from backend.observation import run_lifecycle_scan
from backend.opportunity import scan_opportunities

_DEFAULT_MARKET_STATE = MarketStateSchema(
    current_regime="CALM_BULL", spy_price=758.0, spy_sma20=750.0, vix_close=14.5,
    spy_daily_return=0.005, catalyst_dates=["2026-06-08"],
)


async def run_evening_scan(
    db: AsyncSession,
    *,
    force: bool = False,
    today: Optional[date] = None,
) -> EveningScanResponse:
    _today = today or date.today()
    _today_str = _today.isoformat()

    scan_result = await db.execute(select(SessionScanStateModel).filter_by(id=1))
    scan_state = scan_result.scalar_one_or_none()

    if not force and scan_state is not None and scan_state.last_scan_date == _today_str:
        return EveningScanResponse(ran=False, state=scan_state.to_schema())

    config_result = await db.execute(select(PortfolioConfigModel).filter_by(id=1))
    config_model = config_result.scalar_one_or_none()
    if config_model is None:
        raise LookupError("Portfolio config not found — init_db() should always seed one.")
    config = config_model.to_schema()

    market_result = await db.execute(select(MarketStateModel).filter_by(id=1))
    market_model = market_result.scalar_one_or_none()
    market_state = market_model.to_schema() if market_model else _DEFAULT_MARKET_STATE

    # ---- Step 1: live market fetch (best-effort) ----
    market_fetch_status = "UNCONFIGURED"
    if is_configured():
        telemetry = fetch_market_telemetry()
        if telemetry is None:
            market_fetch_status = "FAILED"
        else:
            if market_model is None:
                market_model = MarketStateModel(id=1)
                db.add(market_model)

            existing_ivrs = market_model.underlying_ivrs or {}
            existing_catalysts = market_model.catalyst_dates or []

            winning_regime, scores = compute_regime(
                spy_price=telemetry["spy_price"],
                spy_sma20=telemetry["spy_sma20"],
                vix_close=telemetry["vix_close"],
                underlying_ivrs=existing_ivrs,
                spy_daily_return=telemetry["spy_daily_return"],
                catalyst_dates=existing_catalysts,
                today=_today,
            )

            market_model.spy_price = telemetry["spy_price"]
            market_model.spy_sma20 = telemetry["spy_sma20"]
            market_model.vix_close = telemetry["vix_close"]
            market_model.spy_daily_return = telemetry["spy_daily_return"]
            market_model.current_regime = winning_regime
            market_model.regime_scores = {k: float(v) for k, v in scores.items()}
            market_fetch_status = "OK"
            market_state = market_model.to_schema()

    # ---- Step 2: position price refresh (best-effort) ----
    pos_result = await db.execute(select(PositionModel).filter_by(status="OPEN"))
    open_position_models = pos_result.scalars().all()

    if not open_position_models:
        position_refresh_status = "OK"
    elif not is_configured():
        position_refresh_status = "UNCONFIGURED"
    else:
        occ_symbols = []
        for pos in open_position_models:
            for leg in pos.legs:
                occ_symbols.append(format_occ_symbol(
                    underlying=pos.underlying,
                    expiration=leg["expiration"],
                    option_type=leg["option_type"],
                    strike=leg["strike"],
                ))

        quotes = fetch_options_latest_quotes(occ_symbols)
        if not quotes:
            position_refresh_status = "FAILED"
        else:
            for pos in open_position_models:
                leg_prices_fetched = True
                long_val = 0.0
                short_val = 0.0
                for leg in pos.legs:
                    occ_sym = format_occ_symbol(
                        underlying=pos.underlying,
                        expiration=leg["expiration"],
                        option_type=leg["option_type"],
                        strike=leg["strike"],
                    )
                    if occ_sym in quotes:
                        price = quotes[occ_sym]
                        if leg["direction"] == "LONG":
                            long_val += price
                        else:
                            short_val += price
                    else:
                        leg_prices_fetched = False
                        break

                if leg_prices_fetched:
                    if pos.premium_direction == "DEBIT":
                        new_val = long_val - short_val
                    else:
                        new_val = short_val - long_val
                    pos.current_value_per_share = round(new_val, 2)
            position_refresh_status = "OK"

    await db.commit()

    # ---- Step 3: Layer A lifecycle counts ----
    pos_result = await db.execute(select(PositionModel).filter_by(status="OPEN"))
    open_positions = [p.to_schema() for p in pos_result.scalars().all()]

    p1_count = 0
    p2_count = 0
    for pos in open_positions:
        scan = run_lifecycle_scan(
            pos,
            current_regime=market_state.current_regime,
            spy_price=market_state.spy_price,
            catalyst_dates=market_state.catalyst_dates or [],
            today=_today,
        )
        if scan["priority"] == "P1 — CLOSE NOW":
            p1_count += 1
        elif scan["priority"].startswith("P2"):
            p2_count += 1

    # ---- Step 4: Layer C opportunity count ----
    pb_result = await db.execute(select(PlaybookDefinitionModel))
    playbooks = [pb.to_schema() for pb in pb_result.scalars().all()]

    opportunity_result = scan_opportunities(playbooks, market_state, open_positions, config, today=_today)
    eligible_candidate_count = (
        0 if opportunity_result.portfolio_blocked
        else sum(1 for c in opportunity_result.candidates if c.eligible)
    )

    # ---- Upsert singleton scan-state row ----
    if scan_state is None:
        scan_state = SessionScanStateModel(id=1)
        db.add(scan_state)

    scan_state.last_scan_at = datetime.now(timezone.utc).isoformat()
    scan_state.last_scan_date = _today_str
    scan_state.p1_count = p1_count
    scan_state.p2_count = p2_count
    scan_state.eligible_candidate_count = eligible_candidate_count
    scan_state.market_fetch_status = market_fetch_status
    scan_state.position_refresh_status = position_refresh_status

    await db.commit()
    await db.refresh(scan_state)

    return EveningScanResponse(ran=True, state=scan_state.to_schema())
