"""trading_control.py — the operational kill switch (ADR-0008, spec/supervision.md, #65).

Distinct from the per-trade Common Sense Kill Switch: this is a persisted
STATE checked synchronously immediately before every placeOrder, regardless
of trade validity. One choke point all orders pass through.

Fail-closed by construction (each default pinned by a test):
- control row missing, unreadable, or unrecognized → HALT_ENTRIES
- the sentinel file (HALT in the project root) overrides everything
- halts LATCH — resuming requires `allow_resume=True`. The console endpoint
  passes it for an operator RESUME; the ntfy remote channel can only HALT.
  anomaly.py's self-clear (#927) also passes it, narrowly: only to lift its
  OWN prior anomaly-actor halt when the rule that set it stops finding
  evidence — never an operator/ntfy halt, never a downgrade of
  FLATTEN_REQUESTED.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditEventModel, TradingControlModel

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
HALT_ENTRIES = "HALT_ENTRIES"
FLATTEN_REQUESTED = "FLATTEN_REQUESTED"
VALID_STATES = frozenset({ACTIVE, HALT_ENTRIES, FLATTEN_REQUESTED})

GLOBAL_SCOPE = "GLOBAL"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TradingHaltedError(RuntimeError):
    """Raised at the order choke point when entries are not allowed."""

    def __init__(self, scope: str, state: str) -> None:
        super().__init__(f"Entries blocked: {scope} is {state}")
        self.scope = scope
        self.state = state


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sentinel_path() -> Path:
    """Zero-dependency override for when the DB or UI is itself broken."""
    return Path(os.getenv("HALT_FILE", str(_PROJECT_ROOT / "HALT")))


def sentinel_halt_active() -> bool:
    return sentinel_path().exists()


async def _write_audit(session: AsyncSession, event_type: str, book_id: str | None, actor: str, payload: dict) -> None:
    session.add(AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor=actor, payload=payload))


async def get_control_state(session: AsyncSession, scope: str) -> str:
    """Raw state for one scope. Fail-closed: anything abnormal reads as HALT_ENTRIES.

    #464 (Audit II R3 F1): this is THE choke-point read, called synchronously
    immediately before every placeOrder — the one place a console HALT posted
    mid-run MUST land. `session.get` is an identity-map lookup: the
    executor's long-lived session loads every control row once at Layer A
    start, so every later call here was a cache hit that emitted no SQL and
    could never see a console write from another process. populate_existing
    forces a real SELECT and overwrites the cached instance with the fresh
    row on every call.
    """
    try:
        result = await session.execute(
            select(TradingControlModel)
            .where(TradingControlModel.scope == scope)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("trading_control unreadable for %s: %s — failing closed", scope, exc)
        return HALT_ENTRIES
    if row is None or row.state not in VALID_STATES:
        return HALT_ENTRIES
    return row.state


async def check_trading_control(session: AsyncSession, book_id: str | None = None) -> tuple[str, str]:
    """Effective (scope, state) for order submission — most restrictive wins.

    Precedence: sentinel file > GLOBAL row > book row. A book without its own
    control row is halted (fail-closed); book creation must seed the row.
    """
    if sentinel_halt_active():
        return ("SENTINEL", HALT_ENTRIES)
    global_state = await get_control_state(session, GLOBAL_SCOPE)
    if global_state != ACTIVE:
        return (GLOBAL_SCOPE, global_state)
    if book_id is not None:
        book_state = await get_control_state(session, book_id)
        if book_state != ACTIVE:
            return (book_id, book_state)
    return (GLOBAL_SCOPE, ACTIVE)


async def assert_entries_allowed(session: AsyncSession, book_id: str | None = None, actor: str = "executor") -> None:
    """THE choke point: call synchronously immediately before placeOrder.

    Logs the control-state value it read to audit_events on every call
    (spec/supervision.md → Enforcement point), then raises TradingHaltedError
    unless the effective state is ACTIVE.
    """
    scope, state = await check_trading_control(session, book_id)
    await _write_audit(session, "CONTROL_CHECK", book_id, actor, {"scope_read": scope, "state_read": state})
    if state != ACTIVE:
        raise TradingHaltedError(scope, state)


async def set_control(
    session: AsyncSession,
    scope: str,
    state: str,
    reason: str,
    actor: str,
    *,
    allow_resume: bool = False,
) -> TradingControlModel:
    """Transition a scope's control state, with an audit event.

    Halts latch: state=ACTIVE requires allow_resume=True. The console path
    passes it for operator RESUME; anomaly.py's self-clear (#927) also
    passes it, but only to lift its own prior anomaly-actor halt once that
    halt's rule stops finding evidence — never the ntfy channel, and never a
    generic "automation may resume" escape hatch.
    """
    if state not in VALID_STATES:
        raise ValueError(f"Unknown trading-control state {state!r}")
    if state == ACTIVE and not allow_resume:
        raise PermissionError("RESUME is console-only — this caller may only move toward safety")
    row = await session.get(TradingControlModel, scope)
    if row is None:
        row = TradingControlModel(scope=scope, state=state, reason=reason, actor=actor, changed_at=_now())
        session.add(row)
    else:
        row.state = state
        row.reason = reason
        row.actor = actor
        row.changed_at = _now()
    await _write_audit(
        session,
        "CONTROL_STATE_CHANGED",
        None if scope == GLOBAL_SCOPE else scope,
        actor,
        {"scope": scope, "state": state, "reason": reason},
    )
    await session.commit()
    return row


async def _ntfy_watermark(session: AsyncSession) -> int | None:
    """Unix time of the last processed command message (#278, audit H7)."""
    row = (
        await session.execute(
            select(AuditEventModel)
            .filter_by(event_type="NTFY_COMMANDS_POLLED")
            .order_by(AuditEventModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    watermark = row.payload.get("watermark")
    return int(watermark) if watermark is not None else None


async def apply_ntfy_commands(session: AsyncSession) -> int:
    """Poll the ntfy command topic and apply HALT commands (asymmetric channel).

    Accepts exactly one command: 'HALT' (global) or 'HALT <book_id>'. RESUME
    over ntfy is ignored and audited — a leaked topic can only move the
    system toward safety. Returns the number of halts applied.

    A persisted watermark (#278, audit H7) makes each command apply exactly
    once: without it, the 24h lookback re-applied a HALT the operator had
    already resumed from the console — every night, silently. An applied
    HALT pushes a receipt to the main topic, so the phone that sent the
    command hears it landed.
    """
    topic = os.getenv("NTFY_COMMAND_TOPIC")
    if not topic:
        return 0
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh")
    watermark = await _ntfy_watermark(session)
    since = str(watermark + 1) if watermark else "24h"
    try:
        resp = await asyncio.to_thread(
            httpx.get, f"{server}/{topic}/json", params={"poll": "1", "since": since}, timeout=15.0
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("ntfy command poll failed: %s", exc)
        return 0

    applied = 0
    newest = watermark or 0
    seen = 0
    for line in resp.text.splitlines():
        parsed = _parse_ntfy_message(line)
        if parsed is None:
            continue
        message, msg_time = parsed
        if watermark and msg_time <= watermark:
            continue  # belt and braces — 'since' should already exclude these
        seen += 1
        newest = max(newest, msg_time)
        parts = message.strip().split()
        if not parts:
            continue
        command = parts[0].upper()
        scope = parts[1] if len(parts) > 1 else GLOBAL_SCOPE
        if command == "HALT":
            await set_control(session, scope, HALT_ENTRIES, reason=f"ntfy remote command: {message!r}", actor="ntfy")
            applied += 1
            from backend.operator import send_ntfy  # local import — avoids a cycle

            send_ntfy("basis: remote HALT applied", f"Scope {scope} halted by ntfy command {message!r}", "high")
        elif command == "RESUME":
            await _write_audit(session, "CONTROL_RESUME_REMOTE_IGNORED", None, "ntfy", {"message": message})
            await session.commit()
            logger.warning("RESUME over ntfy ignored (console-only): %r", message)
    if seen:
        await _write_audit(session, "NTFY_COMMANDS_POLLED", None, "ntfy", {"watermark": newest, "messages": seen})
        await session.commit()
    return applied


def _parse_ntfy_message(line: str) -> tuple[str, int] | None:
    import json

    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if payload.get("event") != "message":
        return None
    return str(payload.get("message", "")), int(payload.get("time", 0))
