"""performance.py — benchmark and risk-adjusted analytics (#9).

Invariants (spec/domain-rules.md): never report a percentage without its
sample size, never fabricate sample data. Every metric here returns None
until the sample supports it — an honest "insufficient data" beats a
confident number computed from three trades.

Definitions:
- Per-trade return = realized P&L / capital at risk (max loss × 100 ×
  contracts). Playbook-level CAGR compounds those returns and annualizes
  over the first-entry→last-exit span — "CAGR on capital at risk", which
  needs no external account-NAV assumption.
- Sharpe = mean/std of per-trade returns × sqrt(trades per year), risk-free
  rate treated as zero.
- Max drawdown = peak-to-trough of cumulative realized P&L in exit order,
  in dollars (same convention as the console's per-book metric).
- SPY benchmark CAGR comes from the stored index_history closes (persisted
  nightly by the operator); BXM has no free data source and stays None.
"""

import datetime
import math
from collections import defaultdict
from dataclasses import dataclass

from backend.models import (
    BenchmarkData,
    ClosurePostMortemModel,
    PerformanceDiagnosticsSchema,
    PlaybookMetrics,
    PositionModel,
)

MIN_TRADES_FOR_RISK_METRICS = 10
MIN_SPAN_DAYS = 30
MIN_BENCHMARK_SPAN_DAYS = 180


@dataclass(frozen=True)
class TradeRecord:
    entry_date: str  # ISO
    exit_date: str  # ISO
    realized_pnl: float  # dollars
    capital_at_risk: float  # dollars


@dataclass(frozen=True)
class RiskMetrics:
    cagr: float | None
    sharpe: float | None
    max_drawdown: float | None  # dollars; computable from the first closed trade


def _parse(date_str: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except ValueError:
        return None


def spy_benchmark_cagr(closes: list[tuple[str, float]]) -> float | None:
    """Annualized SPY return over the stored history window.

    Requires ≥180 days of span — annualizing a short window manufactures a
    misleading number.
    """
    rows = sorted((r for r in closes if r[1] > 0), key=lambda r: r[0])
    if len(rows) < 2:
        return None
    first, last = _parse(rows[0][0]), _parse(rows[-1][0])
    if first is None or last is None:
        return None
    span_days = (last - first).days
    if span_days < MIN_BENCHMARK_SPAN_DAYS:
        return None
    total_return = rows[-1][1] / rows[0][1]
    return round(total_return ** (365.0 / span_days) - 1.0, 4)


def compute_risk_metrics(trades: list[TradeRecord]) -> RiskMetrics:
    ordered = sorted(trades, key=lambda t: t.exit_date)

    # Max drawdown is a dollar figure over the actual trade sequence — honest
    # at any sample size.
    equity = peak = drawdown = 0.0
    for trade in ordered:
        equity += trade.realized_pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    max_dd = round(drawdown, 2) if ordered else None

    returns = [t.realized_pnl / t.capital_at_risk for t in ordered if t.capital_at_risk > 0]
    entry_dates = [d for t in ordered if (d := _parse(t.entry_date)) is not None]
    exit_dates = [d for t in ordered if (d := _parse(t.exit_date)) is not None]
    if len(returns) < MIN_TRADES_FOR_RISK_METRICS or not entry_dates or not exit_dates:
        return RiskMetrics(cagr=None, sharpe=None, max_drawdown=max_dd)
    span_days = (max(exit_dates) - min(entry_dates)).days
    if span_days < MIN_SPAN_DAYS:
        return RiskMetrics(cagr=None, sharpe=None, max_drawdown=max_dd)

    # CAGR on capital at risk: compound the per-trade returns, annualize.
    multiplier = 1.0
    for r in returns:
        multiplier *= 1.0 + r
    cagr = round(multiplier ** (365.0 / span_days) - 1.0, 4) if multiplier > 0 else None

    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    if std <= 0:
        sharpe = None  # zero dispersion is a degenerate sample, not infinite skill
    else:
        trades_per_year = n * 365.0 / span_days
        sharpe = round(mean / std * math.sqrt(trades_per_year), 2)

    return RiskMetrics(cagr=cagr, sharpe=sharpe, max_drawdown=max_dd)


def compose_diagnostics(
    post_mortems: list[ClosurePostMortemModel],
    positions_by_id: dict[str, PositionModel],
    spy_closes: list[tuple[str, float]],
    generated_at: str,
) -> PerformanceDiagnosticsSchema:
    """Per-playbook performance metrics plus the SPY benchmark (#179). Pure
    composition over already-loaded rows — the route loads and returns."""
    groups: dict[tuple[str, str], list[ClosurePostMortemModel]] = defaultdict(list)
    for pm in post_mortems:
        pb_id = pm.playbook_id or "MANUAL_TRADE"
        pb_ver = pm.playbook_version or "N/A"
        groups[(pb_id, pb_ver)].append(pm)

    playbook_metrics = []
    for (pb_id, pb_ver), pms in groups.items():
        total = len(pms)
        wins = sum(1 for pm in pms if pm.outcome == "WIN")
        win_rate = wins / total if total > 0 else None

        total_profit = sum(pm.realized_pnl for pm in pms if pm.realized_pnl > 0)
        total_loss = abs(sum(pm.realized_pnl for pm in pms if pm.realized_pnl < 0))
        profit_factor = (total_profit / total_loss) if total_loss > 0 else None

        returns_on_risk = []
        trades: list[TradeRecord] = []
        for pm in pms:
            pos = positions_by_id.get(pm.position_id)
            if pos and pos.max_loss and pos.max_loss > 0:
                returns_on_risk.append(pm.realized_pnl / pos.max_loss)
                trades.append(
                    TradeRecord(
                        entry_date=pos.entry_date,
                        exit_date=pm.exit_date,
                        realized_pnl=pm.realized_pnl,
                        capital_at_risk=pos.max_loss * 100 * pos.contracts,
                    )
                )
        avg_ror = sum(returns_on_risk) / len(returns_on_risk) if returns_on_risk else None
        risk = compute_risk_metrics(trades)

        playbook_metrics.append(
            PlaybookMetrics(
                playbook_id=pb_id,
                playbook_version=pb_ver,
                total_trades=total,
                win_rate=win_rate,
                profit_factor=profit_factor,
                avg_return_on_risk=avg_ror,
                cagr=risk.cagr,
                max_drawdown=risk.max_drawdown,
                sharpe=risk.sharpe,
            )
        )

    # SPY benchmark from the nightly-persisted index history; BXM has no
    # free data source and stays None.
    spy_cagr = spy_benchmark_cagr(spy_closes)
    if spy_cagr is not None:
        note = f"SPY benchmark from stored index history ({len(spy_closes)} daily closes); BXM unavailable (no data source)"
    elif spy_closes:
        note = (
            f"SPY history too short to annualize ({len(spy_closes)} closes, need ≥{MIN_BENCHMARK_SPAN_DAYS} days span)"
        )
    else:
        note = "No benchmark data yet — SPY history populates when the nightly operator runs"

    return PerformanceDiagnosticsSchema(
        generated_at=generated_at,
        playbook_metrics=playbook_metrics,
        benchmarks=BenchmarkData(spy_cagr=spy_cagr, note=note),
    )
