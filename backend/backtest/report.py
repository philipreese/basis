"""report.py — plain-text per-run backtest report (#796 PR-4).

ADR-0015 §3: a result presented without its position in the run log is not
evidence — so the header LEADS with that position ("run N; M prior runs on
subject X") before any outcome number appears. The body carries the date
range, the declared-assumption set stamped on the run (what was assumed AT
THE TIME, not what current code assumes), the replay counters, per-book
cash against its starting basis, the position outcomes, and the
abandonment/staleness counts — there are no silent caps: if entries were
abandoned, the report says how many and why the count exists.

Same separation as the sibling modules: no imports from backend.console,
backend.evidence, or backend.database.
"""

from __future__ import annotations

from backend.backtest.runlog import RunLog, RunLogError, RunRecord

#: Counter fields whose non-zero values the report must call out explicitly
#: (never a silent cap): abandonments and staleness.
_LOUD_COUNTERS = {
    "entries_abandoned": "entries abandoned (unpriceable/unlisted at fill time — never silently retried)",
    "closes_abandoned": "closes abandoned for a day (re-triggered by the next lifecycle scan)",
    "stale_marks": "position-days marked stale (prior mark kept; a mid is never synthesized)",
    "stale_telemetry_days": "trading days with stale telemetry (entries blocked)",
}


def _realized_line(trade: dict[str, object]) -> str:
    """One outcomes-table row: entry vs exit/settle and realized per share.

    Realized per share follows the stored-value convention (driver.py /
    executor._order_to_position): current_value_per_share is the position's
    own value (DEBIT) or its buyback/settle cost (CREDIT), so
    CREDIT realizes entry - current and DEBIT realizes current - entry.
    OPEN rows show the same difference as unrealized (mark-based).
    """
    entry = float(trade["entry_premium"])  # type: ignore[arg-type]
    current = float(trade["current_value_per_share"])  # type: ignore[arg-type]
    direction = str(trade["premium_direction"])
    status = str(trade["status"])
    realized = entry - current if direction == "CREDIT" else current - entry
    tag = "unrealized" if status == "OPEN" else "realized"
    stale = int(trade["stale_marks"])  # type: ignore[arg-type]
    stale_note = f"  [{stale} stale marks]" if stale else ""
    return (
        f"  {trade['position_id']}  {trade['book_id']}  {trade['underlying']}  "
        f"{trade['strategy_type']}  {status}  entry {trade['entry_date']} @ {entry:.2f} {direction}"
        f" -> {status.lower()} @ {current:.2f} (exp {trade['expiration_date']})  "
        f"{tag} {realized:+.2f}/share x {trade['contracts']}{stale_note}"
    )


def _header(run: RunRecord, prior_runs: int) -> list[str]:
    return [
        f"run {run.run_number}; {prior_runs} prior runs on subject {run.subject}",
        f"config_hash: {run.config_hash}",
        f"what changed: {run.what_changed}",
        f"date range: {run.date_range}",
        f"started: {run.started_at}   finished: {run.finished_at or 'NOT FINISHED'}",
    ]


def render_report(runlog: RunLog, run_number: int) -> str:
    """The per-run report, log position first (ADR-0015 §3)."""
    run = runlog.run(run_number)
    if run is None:
        raise RunLogError(f"run {run_number} does not exist in {runlog.db_path}")
    lines = _header(run, runlog.prior_run_count(run_number, run.subject))

    lines.append("")
    lines.append("declared assumptions (stamped at run time):")
    for source, text in run.declared_assumptions.items():
        lines.append(f"--- {source} ---")
        lines.extend(f"  {line}" for line in text.strip().splitlines())

    lines.append("")
    lines.append("counters:")
    counters = run.counters or {}
    for name, value in counters.items():
        lines.append(f"  {name}: {value}")
    for name, meaning in _LOUD_COUNTERS.items():
        if counters.get(name):
            lines.append(f"  NOTE: {counters[name]} {meaning}")

    lines.append("")
    lines.append("book cash vs starting basis:")
    for book_id, cash in (run.book_cash or {}).items():
        final = cash["final"]
        if "start" in cash:
            start = cash["start"]
            lines.append(f"  {book_id}: start {start:.2f} -> final {final:.2f}  (P&L {final - start:+.2f})")
        else:
            lines.append(f"  {book_id}: final {final:.2f} (starting basis not recorded)")

    trades = runlog.trades_for_run(run_number)
    lines.append("")
    lines.append(f"position outcomes ({len(trades)}):")
    if trades:
        lines.extend(_realized_line(trade) for trade in trades)
    else:
        lines.append("  (no positions opened)")
    return "\n".join(lines)
