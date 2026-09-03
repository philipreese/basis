"""restore_drill.py — automated restore drill, recon-only mode (#640).

Manual chaos drills don't happen; this makes the drill a command instead of
an intention. Two invocations of the SAME analysis:

1. Default (sandboxed): copy a backup basis.YYYY-MM-DD.db into a scratch
   directory and point every DB read at the COPY. Production basis.db (and
   its -wal/-shm siblings) is never opened, sandboxed or not — belt and
   suspenders on top of (2) below.
2. Standalone (`--against-production`): point the same analysis straight at
   the live basis.db, as an operator "what does the system think of the
   broker right now" command.

Both invocations run against the REAL broker (a live Gateway connection —
this is a drill of the real reqAllOpenOrders/reqCompletedOrders/reqExecutions
path, not a mock) and are read-only twice over, structurally, not by
convention:

- The broker: every DB read goes through ReadOnlyBroker, which exposes ONLY
  reconcile/positions/open_orders/executions. Every mutating BrokerSession
  method (place_spread, close_spread, cancel_by_ref, cancel, preview_spread,
  wait_for_terminal) is a defined method here that raises
  MutatingBrokerCallBlockedError unconditionally — and anything NOT even
  defined here (a future BrokerSession method nobody taught this wrapper
  about) raises the same way via __getattr__, so a new mutating method added
  to BrokerSession later is blocked by default, not by someone remembering
  to update a blocklist. A mutating call reaching this wrapper is itself a
  drill finding, not a bug to route around.
- The database: opened via a literal SQLite read-only URI connection
  (mode=ro). This module never calls session.commit() after a write and
  never constructs an engine any other way — a stray write attempt raises at
  the driver, not at code review.

Because nothing here writes to the database, the analysis mirrors
executor.py's _sync_order_states / reconciliation.py's run_reconciliation
classification logic (RESTORE_GAP_UNKNOWN_HELD, GHOST_ORDER, drift kinds,
would-be order verdicts) WITHOUT calling those functions — calling them
would attempt real writes (fill backfill, HALT_ENTRIES, audit rows), which
is exactly what a read-only DB connection is built to refuse.

Gateway lifecycle and tenancy locks are reused exactly as fill_check.py does
(same launch/poll/teardown pieces, same lock-precedence rules) — this drill
takes its own "restore_drill" lock and defers if executor/gateway/fill_check
holds theirs, so it's safe to run any time, including unattended on a
weekend.
"""

import argparse
import glob
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.broker import BrokerSession, ReconcileReport, order_tif
from backend.dates import day_order_session
from backend.models import Base, FillModel, OrderModel, PositionModel, ReconciliationRunModel
from backend.states import ORDER_DAY_EXPIRED_EVENT, ORDER_PENDING_STATUSES

logger = logging.getLogger(__name__)

ORDER_REF_PREFIX = "basis:"
# #674: re-exported alias — the vocabulary lives in backend/states.py now.
PENDING_ORDER_STATUSES = ORDER_PENDING_STATUSES

_MUTATING_BROKER_METHODS = (
    "preview_spread",
    "place_spread",
    "close_spread",
    "cancel_by_ref",
    "cancel",
    "wait_for_terminal",
)


class MutatingBrokerCallBlockedError(RuntimeError):
    """A drill run reached a mutating broker call. This is itself a drill
    finding (the mutation-proofing failed to keep the run read-only) —
    surfaced as an exception so the run stops loudly rather than placing,
    cancelling, or closing anything for real."""


class ReadOnlyBroker:
    """Structural mutation-proofing over a real BrokerSession (#640).

    No __getattr__ fallback delegates to the wrapped session — every method
    this class does not explicitly define is unreachable. The read surface
    (open/close/reconcile/positions/open_orders/executions) is the complete
    allowlist; everything else, known mutating method or not, raises.
    """

    def __init__(self, inner: BrokerSession) -> None:
        self._inner = inner
        self.mutation_attempts: list[str] = []

    # --- lifecycle (not order mutation) -------------------------------
    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- read surface ----------------------------------------------------
    def reconcile(self, refs: list[str], since: str | None = None) -> ReconcileReport:
        return self._inner.reconcile(refs, since=since)

    def positions(self) -> list[Any]:
        return self._inner.positions()

    def open_orders(self) -> list[Any]:
        return self._inner.open_orders()

    def executions(self, since: str | None = None) -> list[Any]:
        return self._inner.executions(since=since)

    # --- explicit blocks for every known mutating method ------------------
    def _blocked(self, name: str, *_args: object, **_kwargs: object) -> None:
        self.mutation_attempts.append(name)
        raise MutatingBrokerCallBlockedError(
            f"restore drill: {name}() is structurally blocked — recon-only mode never mutates the broker "
            "(#640). A code path reaching this is itself a drill finding, not something to route around."
        )

    def preview_spread(self, *args: object, **kwargs: object) -> None:
        self._blocked("preview_spread", *args, **kwargs)

    def place_spread(self, *args: object, **kwargs: object) -> None:
        self._blocked("place_spread", *args, **kwargs)

    def close_spread(self, *args: object, **kwargs: object) -> None:
        self._blocked("close_spread", *args, **kwargs)

    def cancel_by_ref(self, *args: object, **kwargs: object) -> None:
        self._blocked("cancel_by_ref", *args, **kwargs)

    def cancel(self, *args: object, **kwargs: object) -> None:
        self._blocked("cancel", *args, **kwargs)

    def wait_for_terminal(self, *args: object, **kwargs: object) -> None:
        self._blocked("wait_for_terminal", *args, **kwargs)

    # --- defense in depth: anything undefined is blocked too --------------
    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attributes not already resolved above
        # (defined methods, self._inner, self.mutation_attempts) — so this
        # is exactly the "anything else" case: a BrokerSession method this
        # wrapper was never taught about. Default to blocked, not forwarded.
        def _unknown(*_args: object, **_kwargs: object) -> None:
            self._blocked(name)

        return _unknown


# ---------------------------------------------------------------------------
# Backup selection / sandbox staging
# ---------------------------------------------------------------------------


def default_backup_dir() -> Path:
    return Path(os.getenv("DB_BACKUP_DIR", str(Path.home() / "OneDrive" / "basis-db-backups")))


def find_oldest_backup(backup_dir: Path, stem: str = "basis") -> Path | None:
    """The oldest `{stem}.YYYY-MM-DD.db` rotation in backup_dir — deliberately
    the OLDEST, not the newest: a stale copy is what actually exercises the
    restore-gap detection paths (#542) this drill exists to prove."""
    matches = sorted(glob.glob(str(backup_dir / f"{stem}.????-??-??.db")))
    return Path(matches[0]) if matches else None


def stage_sandbox_copy(backup: Path, scratch_dir: Path) -> Path:
    """Copy a backup file (and any -wal/-shm siblings) into scratch_dir.
    Backups are already consistent single-file snapshots (db_backup.py's
    _snapshot_sqlite uses SQLite's own backup API to produce them), so a
    plain file copy is a faithful, non-destructive staging step."""
    dest = scratch_dir / backup.name
    shutil.copy2(backup, dest)
    for suffix in ("-wal", "-shm"):
        sibling = backup.with_name(backup.name + suffix)
        if sibling.exists():
            shutil.copy2(sibling, scratch_dir / sibling.name)
    return dest


# ---------------------------------------------------------------------------
# Sandbox migration (#646) — real restore semantics: a restored backup gets
# migrated by init_db on next startup, THEN the pipeline reconciles. Only the
# SCRATCH COPY is ever opened read-write; the broker wrapper and the
# --against-production read-only connection are unchanged.
# ---------------------------------------------------------------------------

# Audit event types init_db/_ensure_schema_sync writes for a migration —
# reported here as the drill's own migration section (#646). Anything else
# init_db might one day start writing is simply not surfaced by name; the
# tables/columns-added diff below still catches unnamed schema changes.
_MIGRATION_AUDIT_EVENT_TYPES = (
    "BOOK_CONFIG_SYNCED",
    "POST_MORTEM_DUPLICATE_QUARANTINED",
    "TEST_POLLUTION_QUARANTINED",
    "DATABASE_RENAMED",
)


@dataclass
class MigrationOutcome:
    ok: bool
    tables_added: list[str] = field(default_factory=list)
    columns_added: dict[str, list[str]] = field(default_factory=dict)
    audit_rows: list[dict] = field(default_factory=list)
    error: str = ""


def _sqlite_schema_snapshot(db_path: Path, readonly: bool = False) -> dict[str, set[str]]:
    """{table_name: {column_name, ...}} via raw sqlite3 — a plain read.
    Safe to run against the sandbox copy at any point, migrated or not
    (readonly=False there — it's a disposable scratch file about to be
    migrated read-write regardless). readonly=True opens the SAME literal
    mode=ro URI connection style readonly_session_maker uses for
    --against-production — a write attempt raises at the driver, never
    silently no-ops, so this pre-flight can never itself be the write
    that touches the production file (#739)."""
    if readonly:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return {t: {row[1] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()} for t in tables}
    finally:
        conn.close()


def _expected_schema_snapshot() -> dict[str, set[str]]:
    """{table_name: {column_name, ...}} from the ORM metadata (backend.models.Base)
    — the schema init_db/_ensure_schema_sync (backend/database.py) converges
    every table toward via additive ALTER TABLE. This is a pure in-memory
    read of the model definitions, no DB access at all."""
    return {table.name: {c.name for c in table.columns} for table in Base.metadata.sorted_tables}


@dataclass
class SchemaDriftCheck:
    ok: bool
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def gap_count(self) -> int:
        """Not a count of NAMED migrations (this codebase has none — see
        database.py's additive-ALTER convergence, not a migration-file
        chain) — the number of missing tables/columns, the same unit
        migrate_sandbox_copy already reports as its own tables_added/
        columns_added diff. A reasonable, honest proxy for "how far behind",
        not a literal migration count."""
        return len(self.missing_tables) + sum(len(cols) for cols in self.missing_columns.values())


def check_production_schema_drift(database_path: Path) -> SchemaDriftCheck:
    """Pre-flight, read-only schema-vs-models comparison for the
    --against-production path (#739) — mirrors migrate_sandbox_copy's
    pre-Gateway bail (#646) for the sandbox path: a plain read, safe to run
    BEFORE taking the restore_drill tenancy lock or launching Gateway.
    Catches the same "a migration-bearing PR merged, but no init_db-calling
    entry point has run against this file yet" gap that migrate_sandbox_copy
    handles for its own scratch copy — production's real fix is always the
    NEXT init_db-calling run (executor/main/flex_audit), never this drill;
    this function only detects and reports the gap. NEVER migrates
    database_path itself: the readonly=True connection makes a write
    attempt here impossible at the driver level, not merely avoided by
    convention — refusing cleanly is correct behavior, only the OLD failure
    shape (an uncaught OperationalError deep inside the analysis, after
    Gateway was already up) was wrong."""
    actual = _sqlite_schema_snapshot(database_path, readonly=True)
    expected = _expected_schema_snapshot()
    missing_tables = sorted(expected.keys() - actual.keys())
    missing_columns = {
        table: sorted(cols - actual.get(table, set()))
        for table, cols in expected.items()
        if table not in missing_tables and (cols - actual.get(table, set()))
    }
    return SchemaDriftCheck(
        ok=not missing_tables and not missing_columns,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
    )


def migrate_sandbox_copy(sandbox_db: Path, repo_root: Path | None = None) -> MigrationOutcome:
    """Run the REAL backend.database.init_db() against the sandbox copy,
    read-write, in a fresh subprocess — this drill's own process never
    imports backend.database bound to a writable engine, so its read-only
    guarantees for the broker and for --against-production stay untouched.
    A subprocess with DATABASE_URL pointed at the copy is also the most
    faithful mirror of real restore semantics: production restores a backup
    file, then the NEXT process start (a fresh interpreter, module state
    bound at import) is what migrates it.

    This is itself valuable drill coverage (#646): it exercises the
    migration path — additive ALTERs, the closure_post_mortems dupe
    quarantine, the test-pollution quarantine, seed/config sync — against a
    genuinely old schema, which the normal nightly/console entrypoints
    never do (their databases are already current). A migration failure
    here is exactly the kind of restore-day surprise this drill exists to
    surface, so it is reported as a run error, not swallowed.
    """
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    before = _sqlite_schema_snapshot(sandbox_db)
    migration_started_at = datetime.now(UTC).isoformat()

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{sandbox_db.resolve().as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-c", "import asyncio; from backend.database import init_db; asyncio.run(init_db())"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        return MigrationOutcome(ok=False, error=(proc.stderr or proc.stdout or "init_db exited nonzero").strip())

    after = _sqlite_schema_snapshot(sandbox_db)
    tables_added = sorted(after.keys() - before.keys())
    columns_added = {
        table: sorted(cols - before.get(table, set()))
        for table, cols in after.items()
        if table in before and (cols - before[table])
    }

    conn = sqlite3.connect(str(sandbox_db))
    try:
        placeholders = ",".join("?" for _ in _MIGRATION_AUDIT_EVENT_TYPES)
        rows = conn.execute(
            f"SELECT run_at, book_id, event_type, payload FROM audit_events "
            f"WHERE event_type IN ({placeholders}) AND run_at >= ? ORDER BY id",
            (*_MIGRATION_AUDIT_EVENT_TYPES, migration_started_at),
        ).fetchall()
    finally:
        conn.close()
    audit_rows = [{"run_at": r[0], "book_id": r[1], "event_type": r[2], "payload": r[3]} for r in rows]

    return MigrationOutcome(ok=True, tables_added=tables_added, columns_added=columns_added, audit_rows=audit_rows)


# ---------------------------------------------------------------------------
# Read-only DB access
# ---------------------------------------------------------------------------


@contextmanager
def readonly_session_maker(db_path: Path):
    """A session_maker bound to db_path through a literal SQLite read-only
    URI connection (mode=ro) — a write attempt raises at the driver, the
    same structural guarantee ReadOnlyBroker gives the broker side. This
    never touches backend.database's module-level engine (which does
    import-time file migration and is bound to whatever DATABASE_URL was
    set at process start) — it opens its own, explicit, disposable engine."""
    url = f"sqlite+aiosqlite:///file:{db_path.resolve().as_posix()}?mode=ro&uri=true"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield maker
    finally:
        import asyncio

        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Analysis (pure reads only — see module docstring for why this doesn't call
# the mutating executor.py/reconciliation.py machinery directly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderVerdict:
    order_ref: str
    db_status: str
    broker_state: str
    verdict: str  # mirrors the real event type _sync_order_states would audit
    detail: str = ""


@dataclass(frozen=True)
class DriftFinding:
    kind: str
    key: str
    broker_qty: float
    expected_qty: float


@dataclass
class DrillReport:
    mode: str  # "sandbox" | "production"
    source_db: str
    sandbox_db: str | None
    run_at: str
    gap_trading_days: int | None  # #650: None means no reconciliation baseline exists at all — treated as maximal
    order_verdicts: list[OrderVerdict] = field(default_factory=list)
    restore_gap_held: list[str] = field(default_factory=list)
    drifts: list[DriftFinding] = field(default_factory=list)
    unknown_ref_exec_ids: list[str] = field(default_factory=list)
    mutation_attempts: list[str] = field(default_factory=list)
    migration: MigrationOutcome | None = None
    error: str | None = None

    @property
    def clean(self) -> bool:
        migration_clean = self.migration is None or (self.migration.ok and not self.migration.audit_rows)
        return (
            not self.order_verdicts_flagged
            and not self.drifts
            and not self.mutation_attempts
            and not self.error
            and migration_clean
        )

    @property
    def order_verdicts_flagged(self) -> list[OrderVerdict]:
        quiet = {"FILLED", "OPEN_UNCHANGED"}
        return [v for v in self.order_verdicts if v.verdict not in quiet]


async def _restore_gap_trading_days(session: AsyncSession, today: date) -> int | None:
    """None means there is no prior reconciliation run to measure a gap
    from at all (#650) — an empty database, or a restore of a
    pre-reconciliation backup. That is NOT "0 trading days"; treating it as
    zero read as "no gap," the most-dangerous-possible default, and let the
    classification below terminalize UNKNOWN verdicts freely on the exact
    run where trust in the ledger is lowest. Only a real prior run yields a
    measured integer gap."""
    from backend.anomaly import _market_days_between

    last_recon = (
        await session.execute(select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    if last_recon is None:
        return None
    return _market_days_between(last_recon.run_at, today.isoformat())


async def _classify_order_sync(
    session: AsyncSession, report: ReconcileReport, restore_gap_trading_days: int | None, today: date
) -> tuple[list[OrderVerdict], list[str]]:
    """Read-only mirror of executor._sync_order_states's classification —
    computes the verdict each pending order WOULD receive, without writing
    it. See module docstring for why this doesn't call the real function."""
    from backend.broker import RefState

    pending = (
        (await session.execute(select(OrderModel).filter(OrderModel.status.in_(PENDING_ORDER_STATUSES))))
        .scalars()
        .all()
    )
    verdicts: list[OrderVerdict] = []
    restore_gap_held: list[str] = []
    for order in pending:
        if order.status == "PARTIAL":
            verdicts.append(
                OrderVerdict(
                    order.order_ref, order.status, "N/A", "ALREADY_LATCHED_PARTIAL", "unchanged: awaits a human"
                )
            )
            continue
        state = report.state(order.order_ref)
        fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
        if state is RefState.FILLED:
            verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "FILLED"))
        elif state is RefState.CANCELLED:
            if fills:
                verdicts.append(
                    OrderVerdict(
                        order.order_ref, order.status, state.value, "WOULD_LATCH_PARTIAL", "executed before cancel"
                    )
                )
                continue
            reason = report.rejection_reason(order.order_ref)
            if reason:
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "ORDER_REJECTED", reason))
            else:
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "CANCELLED"))
        elif state is RefState.UNKNOWN:
            if fills:
                verdicts.append(
                    OrderVerdict(
                        order.order_ref,
                        order.status,
                        state.value,
                        "WOULD_LATCH_PARTIAL",
                        "executed, broker verdict unknown",
                    )
                )
                continue
            if restore_gap_trading_days is None or restore_gap_trading_days > 1:
                restore_gap_held.append(order.order_ref)
                gap_detail = (
                    "no reconciliation baseline exists"
                    if restore_gap_trading_days is None
                    else f"gap {restore_gap_trading_days} trading day(s)"
                )
                verdicts.append(
                    OrderVerdict(
                        order.order_ref,
                        order.status,
                        state.value,
                        "RESTORE_GAP_UNKNOWN_HELD",
                        f"{gap_detail} — reqCompletedOrders/reqExecutions can't see it",
                    )
                )
                continue
            if order.status == "STAGED":
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "INTENT_EXPIRED"))
                continue
            # #959: mirrors executor._sync_order_states's UNKNOWN branch —
            # position-expired, then DAY-session-expired, then genuinely lost.
            pos = await session.get(PositionModel, order.position_id) if order.position_id else None
            if pos is not None and pos.expiration_date and pos.expiration_date <= today.isoformat():
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "ORDER_EXPIRED_AT_BROKER"))
            elif (
                order_tif(order.order_ref) == "DAY"
                and order.submitted_at is not None
                and day_order_session(order.submitted_at) <= today
            ):
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, ORDER_DAY_EXPIRED_EVENT))
            else:
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "ORDER_LOST_AT_BROKER"))
        elif state is RefState.OPEN:
            if fills:
                verdicts.append(
                    OrderVerdict(
                        order.order_ref, order.status, state.value, "WOULD_LATCH_PARTIAL", "resting with a partial fill"
                    )
                )
            elif order.status == "STAGED":
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "STAGED_ORDER_FOUND_RESTING"))
            else:
                verdicts.append(OrderVerdict(order.order_ref, order.status, state.value, "OPEN_UNCHANGED"))
    return verdicts, restore_gap_held


async def run_recon_analysis(
    session_maker: async_sessionmaker,
    broker: ReadOnlyBroker,
    today: date,
    mode: str,
    source_db: str,
    sandbox_db: str | None,
) -> DrillReport:
    """The whole recon-only analysis, read-only twice over — see module
    docstring. Used for both the sandbox drill and the standalone
    against-production command; only the DB/broker wiring differs."""
    from backend.dates import market_today
    from backend.reconciliation import BrokerSnapshot, _classify_drift, _classify_ghost_orders, _expected_leg_quantities

    today = today or market_today()
    async with session_maker() as session:
        gap = await _restore_gap_trading_days(session, today)
        pending_refs = [
            o.order_ref
            for o in (await session.execute(select(OrderModel).filter(OrderModel.status.in_(PENDING_ORDER_STATUSES))))
            .scalars()
            .all()
        ]
        report = broker.reconcile(pending_refs)
        verdicts, restore_gap_held = await _classify_order_sync(session, report, gap, today)

        snapshot = BrokerSnapshot(
            positions=tuple(broker.positions()),
            executions=tuple(broker.executions()),
            open_orders=tuple(broker.open_orders()),
        )
        expected = await _expected_leg_quantities(session, today.isoformat())
        drifts = _classify_drift(snapshot.positions, expected, today.isoformat())
        drifts.extend(await _classify_ghost_orders(session, snapshot.open_orders))

        known_exec_ids = set((await session.execute(select(FillModel.exec_id))).scalars().all())
        unknown_ref_exec_ids = [e.exec_id for e in snapshot.executions if e.exec_id not in known_exec_ids]

    return DrillReport(
        mode=mode,
        source_db=source_db,
        sandbox_db=sandbox_db,
        run_at=_iso_now(),
        gap_trading_days=gap,
        order_verdicts=verdicts,
        restore_gap_held=restore_gap_held,
        drifts=[DriftFinding(d.kind, d.key, d.broker_qty, d.expected_qty) for d in drifts],
        unknown_ref_exec_ids=unknown_ref_exec_ids,
        mutation_attempts=list(broker.mutation_attempts),
    )


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def format_report(report: DrillReport) -> str:
    lines = [
        f"basis restore drill — {report.mode} mode",
        f"run at:      {report.run_at}",
        f"source db:   {report.source_db}",
    ]
    if report.sandbox_db:
        lines.append(f"sandbox db:  {report.sandbox_db}")
    if report.gap_trading_days is None:
        lines.append("restore gap: NO RECONCILIATION BASELINE — treated as maximal (#650), not zero")
    else:
        lines.append(f"restore gap: {report.gap_trading_days} trading day(s) since the last reconciliation run")
    lines.append("")

    if report.migration is not None:
        m = report.migration
        lines.append(f"sandbox migration: {'ok' if m.ok else 'FAILED'}")
        if not m.ok:
            lines.append(f"  ! {m.error}")
        else:
            lines.append(f"  tables added:  {', '.join(m.tables_added) or 'none'}")
            if m.columns_added:
                for table, cols in m.columns_added.items():
                    lines.append(f"  columns added: {table}: {', '.join(cols)}")
            else:
                lines.append("  columns added: none")
            lines.append(f"  quarantine/seed-sync rows: {len(m.audit_rows)}")
            for row in m.audit_rows:
                lines.append(f"    * {row['event_type']} (book {row['book_id']}) at {row['run_at']}: {row['payload']}")
        lines.append("")

    if report.error:
        lines.append(f"RUN ERROR: {report.error}")
        return "\n".join(lines)

    lines.append(f"mutating broker calls attempted: {len(report.mutation_attempts)} (must be 0)")
    for name in report.mutation_attempts:
        lines.append(f"  ! {name}() reached the read-only wrapper — see MutatingBrokerCallBlockedError above")
    lines.append("")

    flagged = report.order_verdicts_flagged
    lines.append(f"order verdicts: {len(report.order_verdicts)} pending order(s), {len(flagged)} flagged")
    for v in report.order_verdicts:
        marker = "  " if v not in flagged else "* "
        detail = f" — {v.detail}" if v.detail else ""
        lines.append(f"{marker}{v.order_ref}: db={v.db_status} broker={v.broker_state} -> {v.verdict}{detail}")
    lines.append("")

    lines.append(f"drift: {len(report.drifts)} finding(s)")
    for d in report.drifts:
        lines.append(f"  * {d.kind} {d.key}: broker={d.broker_qty} expected={d.expected_qty}")
    lines.append("")

    lines.append(f"unrecognized executions (unknown orderRef): {len(report.unknown_ref_exec_ids)}")
    for exec_id in report.unknown_ref_exec_ids:
        lines.append(f"  * {exec_id}")
    lines.append("")

    lines.append(
        "CLEAN — nothing to report" if report.clean else "FINDINGS ABOVE — this is a detection report, not a failure"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gateway lifecycle + tenancy locks (reused exactly as fill_check.py does)
# ---------------------------------------------------------------------------


def _run_with_gateway(work) -> tuple[int, DrillReport | None]:
    """Acquire the drill's own tenancy lock, defer if a nightly/fill-check
    run holds theirs, launch Gateway, run `work(broker)`, tear down exactly
    like fill_check.py's guard does. Returns (exit_code, report|None)."""
    from backend.gateway_lifecycle import (
        GATEWAY_WARMUP_SECONDS,
        PORT_POLL_TIMEOUT_SECONDS,
        _gateway_endpoint,
        launch_gateway,
        stop_gateway,
        wait_for_port,
    )
    from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock

    # #681: checked against run_lock.GATEWAY_TENANT_LOCKS as a whole (not a
    # hand-spelled "executor/gateway/fill_check" subset) — the same
    # centralization this drill's own symmetric checks motivated for its
    # two older neighbors, applied here too so a future fifth tenant is one
    # addition to GATEWAY_TENANT_LOCKS, not a fourth file to remember.
    if other_gateway_tenant_active("restore_drill"):
        logger.warning("A nightly run or fill check holds a tenancy lock — deferring the restore drill")
        return 5, None

    lock = acquire_run_lock("restore_drill")
    if lock is None:
        logger.warning("restore_drill lock held — another drill is already running")
        return 5, None

    start_script = os.getenv("IBC_START_SCRIPT", "")
    if not start_script or not os.path.exists(start_script):
        release_run_lock(lock)
        logger.error("IBC_START_SCRIPT missing — run scripts/setup-ibc.ps1")
        return 2, None

    host, port = _gateway_endpoint()
    proc = None
    try:
        proc = launch_gateway(start_script)
        time.sleep(GATEWAY_WARMUP_SECONDS)
        if not wait_for_port(host, port):
            logger.error("IB Gateway port %s:%s never opened within %ss", host, port, PORT_POLL_TIMEOUT_SECONDS)
            return 3, None
        broker = ReadOnlyBroker(BrokerSession())
        with broker:
            report = work(broker)
        return 0, report
    finally:
        if other_gateway_tenant_active("restore_drill"):
            logger.warning("Another tenant took a lock mid-drill — leaving Gateway up for it")
        elif proc is not None:
            stop_gateway(proc)
        release_run_lock(lock)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def run_sandbox_drill(backup: Path | None = None, backup_dir: Path | None = None, today: date | None = None) -> int:
    """Default mode: copy a backup into a scratch dir, drill against it.
    Production basis.db is never opened — a fresh scratch dir + explicit
    copy is used regardless of what --backup points at."""
    import asyncio

    backup_dir = backup_dir or default_backup_dir()
    chosen = backup or find_oldest_backup(backup_dir)
    if chosen is None or not chosen.exists():
        print(f"No backup found (looked in {backup_dir}, or pass --backup)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="basis-restore-drill-") as scratch:
        sandbox_db = stage_sandbox_copy(chosen, Path(scratch))

        # #646: migrate the SCRATCH COPY read-write before the read-only
        # analysis phase, mirroring real restore semantics (init_db runs on
        # next startup, THEN the pipeline reconciles). A migration failure
        # is itself the finding — bail before ever launching Gateway.
        migration = migrate_sandbox_copy(sandbox_db)
        if not migration.ok:
            report = DrillReport(
                mode="sandbox",
                source_db=str(chosen),
                sandbox_db=str(sandbox_db),
                run_at=_iso_now(),
                gap_trading_days=None,  # never got far enough to measure it
                migration=migration,
                error=f"sandbox migration failed: {migration.error}",
            )
            print(format_report(report))
            return 4

        def work(broker: ReadOnlyBroker) -> DrillReport:
            with readonly_session_maker(sandbox_db) as maker:
                report = asyncio.run(
                    run_recon_analysis(
                        maker, broker, today, mode="sandbox", source_db=str(chosen), sandbox_db=str(sandbox_db)
                    )
                )
                report.migration = migration
                return report

        code, report = _run_with_gateway(work)
        if report is not None:
            print(format_report(report))
        return code


def run_production_recon(database_path: Path, today: date | None = None) -> int:
    """Standalone recon-only command: same analysis, straight against the
    live production DB path through a read-only connection — no copy, no
    write ever attempted (see readonly_session_maker)."""
    import asyncio

    if not database_path.exists():
        print(f"Database not found: {database_path}", file=sys.stderr)
        return 2

    # #739: pre-flight the schema BEFORE the tenancy lock / Gateway launch —
    # mirrors run_sandbox_drill's pre-Gateway migration bail. A migration-
    # bearing PR merging ahead of the next init_db-calling run (executor/
    # main/flex_audit) is normal, expected lag, not a crash — the old
    # behavior discovered it deep inside the analysis, after Gateway was
    # already up and connected to the broker.
    drift = check_production_schema_drift(database_path)
    if not drift.ok:
        print(
            f"production schema is {drift.gap_count} migration(s) behind backend/models.py — "
            "run init_db via any normal entry point (the executor, main.py, or flex_audit — "
            "whichever runs next), or use sandbox mode (drop --against-production) instead.",
            file=sys.stderr,
        )
        if drift.missing_tables:
            print(f"  missing table(s): {', '.join(drift.missing_tables)}", file=sys.stderr)
        for table, cols in drift.missing_columns.items():
            print(f"  missing column(s) in {table}: {', '.join(cols)}", file=sys.stderr)
        return 6

    def work(broker: ReadOnlyBroker) -> DrillReport:
        with readonly_session_maker(database_path) as maker:
            return asyncio.run(
                run_recon_analysis(
                    maker, broker, today, mode="production", source_db=str(database_path), sandbox_db=None
                )
            )

    code, report = _run_with_gateway(work)
    if report is not None:
        print(format_report(report))
    return code


def _default_production_db_path() -> Path:
    from backend.database import DATABASE_URL

    url = DATABASE_URL
    if not url.startswith("sqlite+aiosqlite:///"):
        raise SystemExit(f"--against-production only supports a sqlite DATABASE_URL, got {url!r}")
    return Path(url.removeprefix("sqlite+aiosqlite:///"))


def main(argv: list[str] | None = None) -> int:
    from backend.run_logging import setup_run_logging

    setup_run_logging("restore_drill")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, default=None, help="Explicit backup file (default: oldest rotation)")
    parser.add_argument(
        "--backup-dir", type=Path, default=None, help="Override the backup directory (default: DB_BACKUP_DIR)"
    )
    parser.add_argument(
        "--against-production",
        action="store_true",
        help="Recon-only against the LIVE production DB (read-only connection) instead of a sandboxed backup copy",
    )
    parser.add_argument(
        "--database", type=Path, default=None, help="With --against-production: explicit DB path override"
    )
    args = parser.parse_args(argv)

    if args.against_production:
        db_path = args.database or _default_production_db_path()
        return run_production_recon(db_path)
    return run_sandbox_drill(backup=args.backup, backup_dir=args.backup_dir)


if __name__ == "__main__":
    sys.exit(main())
