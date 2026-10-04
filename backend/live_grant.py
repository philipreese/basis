"""live_grant.py — the operator's live-authority grants (#1065).

Three operator commands, each an attested act in the LIVE database, never
automation:

- grant (STAGE1): give a staked share book live authority at its stage-1
  stake (ADR-0006's #1053 amendment). The operator attests that the stage-1
  entry bar is met; this code checks only what it can check mechanically.
- step-up (STEP_UP): re-record a LIVE book's grant at a larger stake after
  three consecutive clean live monthly rebalances (the #1084 amendment). The
  clean-rebalance count is not computed in code yet: the operator names the
  three month-end signal dates and attests they were clean; this code checks
  they are three consecutive month-ends after the stage-1 grant.
- revoke: a manual demotion (ADR-0014 point 3: an operator may pull a book
  at any time, whatever the policy says). The book also gets a book-scoped
  entries halt.

Every grant records what ADR-0014 point 4 requires: the config hash the book
was granted AS RACED, and the demotion policy version it is judged under
(stage1.DEMOTION_POLICY_VERSION for a stage-1 grant; a step-up keeps its
stage-1 grant's version, "under the same demotion policy version"). The
live executor trades a LIVE book only while its config hash still equals its
latest grant's as-raced hash AND its private live stake still equals the
grant's stake.

Where the stake is pinned (#1098): the stake is private (BASIS_LIVE_STAKE_<id>
in the gitignored `.env.live`), so it is NOT part of the public config or its
hash. The grant row in the live database records it in the clear, and that
row is the pin. A salted hash was the alternative; it was rejected because it
would add a second secret to manage, and the live database already holds the
real cash and holdings, so hashing the stake there hides nothing. The
as-raced config hash stays the public config's hash, so the paper twin's hash
still proves which config was granted.

A grant sets promoted_at (the -30% drawdown window opens there) but never
resumes a halted book: resuming is console-only (ADR-0008), on the live
console.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import live_stake_var, resolve_for_book
from backend.dates import market_date_of
from backend.etf_trend import is_signal_day, next_signal_day_after
from backend.models import AuditEventModel, BookModel, BookMtmHistoryModel, DbMetaModel, LiveGrantModel
from backend.stage1 import DEMOTION_POLICY_VERSION
from backend.states import (
    BOOK_ACTIVE_STATUS,
    LIVE_AUTHORITY_LIVE,
    LIVE_AUTHORITY_REVOKED,
    LIVE_GRANT_STAGE1,
    LIVE_GRANT_STEP_UP,
)
from backend.trading_control import FLATTEN_REQUESTED, HALT_ENTRIES, get_control_state, set_control

ACTOR = "live_grant"
LIVE_AUTHORITY_GRANTED = "LIVE_AUTHORITY_GRANTED"
LIVE_AUTHORITY_STEPPED_UP = "LIVE_AUTHORITY_STEPPED_UP"
LIVE_AUTHORITY_REVOKED_MANUAL = "LIVE_AUTHORITY_REVOKED"
# A step-up needs this many consecutive clean live month-end rebalances.
STEP_UP_CLEAN_REBALANCES = 3
MIN_ATTESTATION_CHARS = 20


class GrantRefused(RuntimeError):
    """The command refused; nothing was written."""


@dataclass(frozen=True)
class GrantResult:
    book_id: str
    kind: str
    grant_id: int
    demotion_policy_version: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _attestation(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) < MIN_ATTESTATION_CHARS:
        raise GrantRefused(
            f"the attestation must be the operator's own sign-off, at least {MIN_ATTESTATION_CHARS} characters"
        )
    return cleaned


async def _assert_live_database(session: AsyncSession) -> None:
    row = await session.get(DbMetaModel, "trading_mode")
    if row is None or row.value != "live":
        raise GrantRefused("this database is not stamped live — grants are recorded in the live database only")


async def _book(session: AsyncSession, book_id: str) -> BookModel:
    book = await session.get(BookModel, book_id)
    if book is None:
        raise GrantRefused(f"no book {book_id!r}")
    if book.status != BOOK_ACTIVE_STATUS:
        raise GrantRefused(f"{book_id} is {book.status}, not ACTIVE")
    return book


async def _latest(session: AsyncSession, book_id: str) -> LiveGrantModel | None:
    return (
        await session.execute(
            select(LiveGrantModel).filter_by(book_id=book_id).order_by(LiveGrantModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


def _staked_share_config(book: BookModel) -> float:
    """The book's private live stake (#1098: BASIS_LIVE_STAKE_<id> in the
    live overlay, never seeds.py). Refuses an options book, an unstaked book,
    and a seeded or malformed stake (resolve_for_book raises)."""
    try:
        config = resolve_for_book(book)
    except (TypeError, ValueError) as exc:
        raise GrantRefused(f"{book.id}'s config does not resolve: {exc}") from exc
    if not config.is_share_book:
        raise GrantRefused(f"{book.id} is an options book — live grants are for stage-1 share books only")
    if config.stage1_stake is None:
        raise GrantRefused(
            f"{book.id} has no private live stake — set {live_stake_var(book.id)} in .env.live before granting"
        )
    return config.stage1_stake


async def grant_stage1(session: AsyncSession, book_id: str, attestation: str, today: date) -> GrantResult:
    """Record a STAGE1 grant. Refuses a book already LIVE (that is a step-up),
    an options book, an unstaked book, and a book with no mark before today —
    the drawdown window opens at the grant and measures from the last mark
    before it, so without one the book would read as halted from night one."""
    text = _attestation(attestation)
    await _assert_live_database(session)
    book = await _book(session, book_id)
    if book.live_authority == LIVE_AUTHORITY_LIVE:
        raise GrantRefused(f"{book_id} is already LIVE — a larger stake is a step-up, not a new grant")
    stake = _staked_share_config(book)
    marks = (await session.execute(select(BookMtmHistoryModel.date).filter_by(book_id=book_id))).scalars().all()
    if not any(d < today.isoformat() for d in marks):
        raise GrantRefused(
            f"{book_id} has no nightly mark before today in the live database — run the live executor (a dry run "
            "is enough) on at least one evening before granting, so the stake window has a baseline"
        )
    now = _now()
    grant = LiveGrantModel(
        book_id=book_id,
        kind=LIVE_GRANT_STAGE1,
        granted_at=now,
        as_raced_config_hash=book.config_hash,
        config_snapshot=dict(book.config or {}),
        stake=stake,
        demotion_policy_version=DEMOTION_POLICY_VERSION,
        attestation=text,
    )
    session.add(grant)
    previous = book.live_authority
    book.live_authority = LIVE_AUTHORITY_LIVE
    book.promoted_at = now
    book.demotion_policy_version = DEMOTION_POLICY_VERSION
    await session.flush()
    session.add(
        AuditEventModel(
            run_at=now,
            book_id=book_id,
            event_type=LIVE_AUTHORITY_GRANTED,
            actor=ACTOR,
            payload={
                "grant_id": grant.id,
                "kind": LIVE_GRANT_STAGE1,
                "previous": previous,
                "as_raced_config_hash": book.config_hash,
                "demotion_policy_version": DEMOTION_POLICY_VERSION,
                "attestation": text,
            },
        )
    )
    await session.commit()
    return GrantResult(book_id, LIVE_GRANT_STAGE1, grant.id, DEMOTION_POLICY_VERSION)


def _check_clean_dates(dates: list[date], since: date, today: date) -> None:
    if len(dates) != STEP_UP_CLEAN_REBALANCES or len(set(dates)) != len(dates):
        raise GrantRefused(f"a step-up names exactly {STEP_UP_CLEAN_REBALANCES} distinct clean month-end signal dates")
    ordered = sorted(dates)
    for d in ordered:
        if not is_signal_day(d):
            raise GrantRefused(f"{d.isoformat()} is not a month-end signal day")
    if ordered[0] <= since:
        raise GrantRefused("every clean rebalance must come after the stage-1 grant")
    if ordered[-1] > today:
        raise GrantRefused("a clean rebalance cannot be in the future")
    for earlier, later in pairwise(ordered):
        if next_signal_day_after(earlier) != later:
            raise GrantRefused("the clean rebalances must be consecutive month-ends (a miss resets the count)")


async def step_up(
    session: AsyncSession, book_id: str, clean_dates: list[date], attestation: str, today: date
) -> GrantResult:
    """Record a STEP_UP grant at the book's current (larger) stake.

    Mechanical checks: the book is LIVE under a recorded grant; its config
    (and config hash) are exactly that grant's, and the private live stake
    in the overlay went up (#1098); three consecutive month-end signal dates after the stage-1
    grant. The cleanliness of those rebalances is the operator's attestation."""
    text = _attestation(attestation)
    await _assert_live_database(session)
    book = await _book(session, book_id)
    if book.live_authority != LIVE_AUTHORITY_LIVE:
        raise GrantRefused(f"{book_id} is not LIVE — a step-up needs a live stage-1 grant to step up from")
    previous = await _latest(session, book_id)
    if previous is None:
        raise GrantRefused(f"{book_id} has no recorded grant to step up from")
    stake = _staked_share_config(book)
    if not stake > previous.stake or math.isclose(stake, previous.stake):
        raise GrantRefused(
            f"the private live stake ({live_stake_var(book_id)}) is not larger than the current grant's stake"
        )
    # #1098: the stake is private, so a step-up changes only the overlay; the
    # public config must be exactly what the previous grant was made on.
    if (book.config or {}) != (previous.config_snapshot or {}) or book.config_hash != previous.as_raced_config_hash:
        raise GrantRefused("the book's config changed since its grant — that is a new config, not a step-up")
    stage1 = (
        await session.execute(
            select(LiveGrantModel)
            .filter_by(book_id=book_id, kind=LIVE_GRANT_STAGE1)
            .order_by(LiveGrantModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if stage1 is None:
        raise GrantRefused(f"{book_id} has no stage-1 grant")
    _check_clean_dates(clean_dates, market_date_of(stage1.granted_at), today)
    now = _now()
    grant = LiveGrantModel(
        book_id=book_id,
        kind=LIVE_GRANT_STEP_UP,
        granted_at=now,
        as_raced_config_hash=book.config_hash,
        config_snapshot=dict(book.config or {}),
        stake=stake,
        demotion_policy_version=previous.demotion_policy_version,
        attestation=text,
        clean_rebalance_dates=[d.isoformat() for d in sorted(clean_dates)],
        previous_grant_id=previous.id,
    )
    session.add(grant)
    book.promoted_at = now  # the -30% halt measures from the new stake and grant date
    book.demotion_policy_version = previous.demotion_policy_version
    await session.flush()
    session.add(
        AuditEventModel(
            run_at=now,
            book_id=book_id,
            event_type=LIVE_AUTHORITY_STEPPED_UP,
            actor=ACTOR,
            payload={
                "grant_id": grant.id,
                "previous_grant_id": previous.id,
                "as_raced_config_hash": book.config_hash,
                "demotion_policy_version": previous.demotion_policy_version,
                "clean_rebalance_dates": grant.clean_rebalance_dates,
                "attestation": text,
            },
        )
    )
    await session.commit()
    return GrantResult(book_id, LIVE_GRANT_STEP_UP, grant.id, previous.demotion_policy_version)


async def revoke(session: AsyncSession, book_id: str, reason: str) -> None:
    """Manual demotion: live_authority REVOKED, plus a book-scoped entries
    halt unless the book is already halted or flattening (never downgrade a
    FLATTEN_REQUESTED). Works on any book state; the live executor stops
    trading the book at its next run. Holdings are not sold — a flatten is a
    separate console act."""
    text = _attestation(reason)
    await _assert_live_database(session)
    book = await session.get(BookModel, book_id)
    if book is None:
        raise GrantRefused(f"no book {book_id!r}")
    previous = book.live_authority
    book.live_authority = LIVE_AUTHORITY_REVOKED
    session.add(
        AuditEventModel(
            run_at=_now(),
            book_id=book_id,
            event_type=LIVE_AUTHORITY_REVOKED_MANUAL,
            actor=ACTOR,
            payload={"previous": previous, "reason": text, "rule": "MANUAL"},
        )
    )
    await session.commit()
    if await get_control_state(session, book_id) not in (HALT_ENTRIES, FLATTEN_REQUESTED):
        await set_control(
            session, book_id, HALT_ENTRIES, reason=f"live authority revoked by the operator: {text}", actor=ACTOR
        )
