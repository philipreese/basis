"""research_brief.py — the AI brief runner, phase 2 of #1131.

    pixi run research-brief                    # nightly (default)
    pixi run research-brief --kind monthly     # the monthly full screen
    pixi run research-brief --kind monthly --check-due   # exit 0/1, no brief written

Design: spec/research-brief.md (ADR-0018). This is the "analyze" half of
snapshot-then-analyze: it reads the latest COMPLETE snapshot of the
requested kind that has not yet been briefed, asks a PINNED model (never a
floating alias — bumping it is a deliberate code change) to shortlist
candidates from ONLY that frozen folder, and writes the result through
backend.research.record_brief, which re-verifies the snapshot's hash before
it will record anything.

The model call goes through the operator's own Claude Code CLI subscription
(`claude -p --model ... --output-format json --json-schema ...`, stdin
carries the prompt), never the paid Anthropic API — the operator ruled no
paid API for this (#1131). `--tools ""` and `--safe-mode` keep the call to
"read the prompt, answer" with no project hooks/CLAUDE.md/skills and no tool
access; `--no-session-persistence` keeps nightly runs from piling transcripts
into the operator's `~/.claude/projects`. `--json-schema` gets BriefOut's own
JSON Schema, so the CLI enforces the shape itself; the reply is re-validated
with `BriefOut.model_validate(..., strict=True)` on this side regardless,
because a schema-shaped reply from an external process is still an untrusted
input until that passes.

The frozen prompt — the system prompt, the per-kind instructions, the
rendering bounds below, and the output schema (never the per-run snapshot
data) — is hashed into `prompt_hash`. Two briefs sharing that hash ran the
same frozen contract, whatever the snapshot. Nothing here places an order or
marks a PICK; that stays strictly with the operator, console-side (phase 3).

Where the design is silent, phase 2 made these choices (stated here, and in
the PR that shipped them, rather than only in a commit message):

- The pinned model is `claude-sonnet-5`, pinned via `--model` on every call
  (never a floating alias like `sonnet`) — not `claude-opus-5`: the CLI
  billing is the operator's flat-rate subscription rather than metered API
  usage, so there is no per-token cost tradeoff driving the choice, but a
  heavier model still means more subscription-quota pressure against other
  interactive use, and the design asks for no more than this task needs.
- No `--fallback-model` is passed to the CLI. A fallback would answer under
  this module's pinned `model_id` while actually having run a different
  model — exactly the floating-alias failure `model_id` exists to rule out.
  The actual model the CLI reports (from its JSON result's `modelUsage`) is
  what gets recorded, falling back to the pinned id only when that's absent.
- Any failure of the CLI call itself — the executable can't be resolved, a
  nonzero exit (not logged in, a usage-limit refusal, a crash), a timeout, or
  a reply that doesn't parse as JSON or doesn't validate against BriefOut —
  is treated as a failed run: nothing is recorded, and it surfaces exactly
  like any other skip (BriefOutcome.alert=True, a "high" priority push),
  never as a silent fallback and never as an uncaught crash.
- A candidate the model names that is not priced in the snapshot is dropped
  (never hallucinated — record_brief would refuse the whole brief for one
  bad ticker) rather than failing the run; the drop is folded into the
  summary so it is never silent. Capping happens BEFORE the unpriced filter,
  so a candidate dropped by the cap never backfills a slot freed by an
  unpriced drop.
- Nightly is capped at NIGHTLY_MAX_CANDIDATES new names and monthly at
  MONTHLY_MAX_CANDIDATES, truncating extras rather than refusing the brief.
- The nightly held-pick check reads each held symbol's ORIGINAL thesis/risks
  /proves-wrong text (from the PICK decision's candidate row) plus its price
  and snapshot price history, not just its ticker — that line is the
  design's only early exit, so the model needs to see it to check it.
- The universe is rendered in full (one compact line per name) rather than
  truncated — a monthly "full screen" that only ever sees the first N
  alphabetically is not a screen. Filings are bounded (MAX_FILINGS,
  MAX_FILING_CHARS) because excerpts are long; held picks' filings are
  always prioritized, then the most recent, never picked by symbol order.
- The monthly "first trading day of the month" trigger has no Task
  Scheduler equivalent at all — `New-ScheduledTaskTrigger` has no monthly
  parameter set — so the monthly task is registered on the same every-
  weekday trigger as nightly, and main() --check-due asks the database
  instead: due when today is a trading day AND no MONTHLY BRIEF exists
  whose snapshot is dated this calendar month (checking for a brief, not
  just a COMPLETE snapshot, so a day whose snapshot finished but whose
  brief step then crashed still retries). That makes a failure on any
  weekday self-correcting on the next one, per the design's "a missed slot
  runs at the next opportunity".
- A brief that cannot run — no snapshot yet, an INCOMPLETE one, an already-
  briefed one, tonight's run never writing a snapshot row at all, or the CLI
  call itself failing — still pushes to the phone (fill_check's precedent:
  "silence would be indistinguishable from the check not running"), at
  "high" priority whenever that's a real problem rather than an ordinary
  already-briefed skip (BriefOutcome.alert).
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import (
    OperatorPickModel,
    ResearchBriefCreate,
    ResearchBriefModel,
    ResearchCandidateCreate,
    ResearchCandidateModel,
    ResearchSnapshotModel,
)
from backend.states import (
    PICK_DECISION_PICK,
    RESEARCH_KIND_MONTHLY,
    RESEARCH_KIND_NIGHTLY,
    RESEARCH_KINDS,
    SNAPSHOT_COMPLETE_STATUS,
)

logger = logging.getLogger(__name__)

# The pinned model. Bumping this is a deliberate code change (ADR-0018: "a
# pinned model, never a floating alias"), reviewed like any other. No
# fallback model is ever passed to the CLI — see the module docstring.
MODEL_ID = "claude-sonnet-5"

# The Claude Code CLI executable. Unset = resolve it with shutil.which at
# call time (see resolve_cli_path) — a Windows Scheduled Task's PATH is
# minimal, so a bare "claude" cannot be assumed to resolve the way it does
# in an interactive shell.
CLI_VAR = "BASIS_CLAUDE_CLI"

# Env vars that would make the CLI bill against the paid Anthropic API
# instead of the operator's subscription login (ADR-0018 update: no paid
# API for this). Stripped from the child process even though nothing in
# this repo is meant to set them anymore — defense against a stray .env
# value outliving this change.
_STRIPPED_ENV_VARS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"})

NIGHTLY_MAX_CANDIDATES = 2  # spec/research-brief.md: "Surface 0-2 new candidates"
MONTHLY_MAX_CANDIDATES = 15  # a token-bounded guard; the design states no cap

# How long to let one `claude -p` call run before giving up. The CLI has no
# max-tokens knob to bound reply length the way the old SDK call did, so
# this is the only backstop against a hung or runaway call. Nightly's
# Scheduled Task budget is 40 minutes total, shared with the snapshot step
# ahead of it; monthly's is 90 minutes for the same reason, and its prompt
# (a full-universe screen) is the heavier of the two.
# (scripts/register-research-brief-task.ps1). Unmeasured against production
# data otherwise — flagged in the PR for the conductor to revisit.
NIGHTLY_CLI_TIMEOUT_SECONDS = 1200
MONTHLY_CLI_TIMEOUT_SECONDS = 4200
CLI_TIMEOUT_BY_KIND = {
    RESEARCH_KIND_NIGHTLY: NIGHTLY_CLI_TIMEOUT_SECONDS,
    RESEARCH_KIND_MONTHLY: MONTHLY_CLI_TIMEOUT_SECONDS,
}

# Filing-excerpt bounds, so one snapshot's digest never blows past a sane
# token budget. The universe itself is rendered in full (see
# render_snapshot_digest) — these bound the filings section only.
MAX_FILINGS = 150
MAX_FILING_CHARS = 2500


class ResearchBriefError(RuntimeError):
    """The brief could not be built or recorded. Nothing was written."""


# ---------------------------------------------------------------------------
# The model's structured output (enforced server-side via messages.parse)
# ---------------------------------------------------------------------------


class BriefCandidateOut(BaseModel):
    """The API's structured-output schema (messages.parse) — kept to plain
    types, with no length constraints, so the generated JSON Schema stays
    simple for the API to enforce. Real validation (non-empty after
    normalizing, length limits) happens in to_candidates against
    ResearchCandidateCreate, which is the schema that actually matters —
    record_brief's."""

    symbol: str
    thesis: str
    risks: str
    proves_wrong: str


class BriefOut(BaseModel):
    summary: str
    candidates: list[BriefCandidateOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The frozen prompt (hashed; never the per-run data)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are the research analyst for basis, an algorithmic trading system's "
    "manual stock-picking program (issue #1131). You read ONLY the frozen "
    "snapshot data given to you in the user message below. You have no web "
    "access and must not rely on any information beyond what is given. "
    "Shortlist US small- and mid-cap common stocks from the universe listed "
    "below for a human operator to review and buy by hand; you never place "
    "an order yourself. Each candidate needs a one-paragraph thesis, its key "
    "risks, and one concrete, falsifiable line describing what would prove "
    "the thesis wrong. Never name a symbol that is not listed in the "
    "universe below — a hallucinated ticker is refused outright. An empty "
    "candidates list is correct and expected on a quiet night."
)

NIGHTLY_INSTRUCTIONS = (
    "This is the NIGHTLY brief. For each held pick below (its original "
    "thesis, risks and proves-wrong line, its price, and its filings in "
    "this snapshot), say in the summary whether the thesis still holds — "
    "the proves-wrong line is the only early exit this program allows, so "
    "check it explicitly. Surface at most "
    f"{NIGHTLY_MAX_CANDIDATES} new candidates, and only when a new filing or "
    "earnings report in this snapshot actually warrants one. Most nights "
    "there is nothing new: return an empty candidates list and a summary "
    "like 'nothing new, theses intact'."
)

MONTHLY_INSTRUCTIONS = (
    "This is the MONTHLY brief: a full screen of the universe below for new "
    "candidates. Shortlist only the strongest names you find — never pad "
    "the list to hit a quota."
)


def frozen_instructions(kind: str) -> str:
    if kind not in RESEARCH_KINDS:
        raise ResearchBriefError(f"unknown brief kind {kind!r}")
    return NIGHTLY_INSTRUCTIONS if kind == RESEARCH_KIND_NIGHTLY else MONTHLY_INSTRUCTIONS


def _frozen_prompt_fingerprint(kind: str) -> dict[str, object]:
    """Everything that makes this kind's prompt what it is, besides the
    per-run snapshot data: the instructions, the rendering bounds, the
    output contract. Changing any of these is a deliberate prompt change
    and should change prompt_hash."""
    return {
        "system": SYSTEM_PROMPT,
        "instructions": frozen_instructions(kind),
        "max_candidates": NIGHTLY_MAX_CANDIDATES if kind == RESEARCH_KIND_NIGHTLY else MONTHLY_MAX_CANDIDATES,
        "max_filings": MAX_FILINGS,
        "max_filing_chars": MAX_FILING_CHARS,
        "output_schema": BriefOut.model_json_schema(),
    }


def prompt_hash_for(kind: str) -> str:
    """sha256 of the frozen prompt fingerprint for *kind* — stable across
    every run of that kind until the prompt itself changes."""
    text = json.dumps(_frozen_prompt_fingerprint(kind), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Rendering the snapshot into the user prompt (pure, testable)
# ---------------------------------------------------------------------------


def _read_json(folder: Path, name: str, default: object) -> object:
    path = folder / name
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _price_line(entry: dict[str, object] | None) -> str:
    if not entry:
        return "no price in this snapshot"
    close = entry.get("close")
    if close is None:
        return "no price in this snapshot"
    history = entry.get("history") or []
    change = ""
    if len(history) >= 2:
        first_close, last_close = history[0][1], history[-1][1]
        if first_close:
            pct = (last_close - first_close) / first_close * 100
            change = f", {pct:+.1f}% over the snapshot's price history"
    return f"close {float(close):.2f}{change}"


def _held_pick_lines(held: list[str], prices: dict[str, object], held_theses: dict[str, dict[str, str]]) -> list[str]:
    lines = [f"Held picks: {', '.join(held) if held else '(none)'}"]
    for symbol in held:
        price_line = _price_line(prices.get(symbol) if isinstance(prices.get(symbol), dict) else None)
        thesis = held_theses.get(symbol)
        if thesis:
            lines.append(
                f"  {symbol} ({price_line}): thesis: {thesis['thesis']} | risks: {thesis['risks']} | "
                f"proves wrong: {thesis['proves_wrong']}"
            )
        else:
            lines.append(f"  {symbol} ({price_line}): no recorded thesis for this symbol")
    return lines


def _prioritized_filings(filings: list[dict], held: set[str]) -> list[dict]:
    """Held picks first, then most recent — never picked by symbol order
    (filings.json is written sorted by symbol, which would otherwise starve
    whichever names sort late, including a held pick's own filings)."""
    by_recency = sorted(filings, key=lambda f: f.get("accepted_at") or f.get("filing_date") or "", reverse=True)
    return sorted(by_recency, key=lambda f: 0 if f.get("symbol") in held else 1)


def render_snapshot_digest(
    folder: Path,
    *,
    held_theses: dict[str, dict[str, str]] | None = None,
    max_universe_rows: int | None = None,
    max_filings: int = MAX_FILINGS,
    max_filing_chars: int = MAX_FILING_CHARS,
) -> str:
    """The frozen folder, as text. The universe is rendered in full by
    default (max_universe_rows=None) — a monthly "full screen" that only
    ever sees the first N alphabetically is not a screen. Filings are
    bounded and prioritized (held picks, then recency). Never raises for a
    missing optional file — an empty section instead, since a COMPLETE
    snapshot may still have an empty held.json."""
    held_theses = held_theses or {}
    held = (_read_json(folder, "held.json", {}) or {}).get("symbols", [])
    universe = (_read_json(folder, "universe.json", {}) or {}).get("members", [])
    filings = (_read_json(folder, "filings.json", {}) or {}).get("filings", [])
    prices = _read_json(folder, "prices.json", {}) or {}

    held_set = set(held)
    lines = _held_pick_lines(held, prices, held_theses)
    lines.append("")

    rows = universe if max_universe_rows is None else universe[:max_universe_rows]
    lines.append(f"Universe: {len(universe)} name(s), showing {len(rows)}")
    lines.append("symbol | name | sector | last_sale | dollar_volume | market_cap")
    for member in rows:
        lines.append(
            f"{member.get('symbol')} | {member.get('name')} | {member.get('sector')} | "
            f"{member.get('last_sale'):.2f} | {member.get('dollar_volume'):.0f} | {member.get('market_cap'):.0f}"
        )
    lines.append("")

    shown = _prioritized_filings(filings, held_set)[:max_filings]
    lines.append(f"Recent filings: {len(filings)}, showing {len(shown)} (held picks first, then most recent)")
    for row in shown:
        lines.append(f"--- {row.get('symbol')} {row.get('form')} filed {row.get('filing_date')} ---")
        excerpt_file = row.get("excerpt_file")
        text = ""
        if excerpt_file:
            try:
                text = (folder / str(excerpt_file)).read_text(encoding="utf-8")[:max_filing_chars]
            except OSError:
                text = "(excerpt unavailable)"
        lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validating the model's structured candidates into record_brief's schema
# ---------------------------------------------------------------------------


def to_candidates(raw_candidates: list[BriefCandidateOut]) -> list[ResearchCandidateCreate]:
    """BriefCandidateOut (the API's enforced shape) to ResearchCandidateCreate
    (record_brief's schema). Usually a pass-through; still validated because
    normalizing (strip + uppercase the symbol) can turn an API-valid value
    into a record_brief-invalid one — e.g. a whitespace-only symbol."""
    out: list[ResearchCandidateCreate] = []
    for i, item in enumerate(raw_candidates):
        try:
            out.append(
                ResearchCandidateCreate(
                    symbol=item.symbol.strip().upper(),
                    thesis=item.thesis.strip(),
                    risks=item.risks.strip(),
                    proves_wrong=item.proves_wrong.strip(),
                )
            )
        except ValidationError as exc:
            raise ResearchBriefError(f"candidate[{i}] ({item.symbol!r}) failed validation: {exc}") from exc
    return out


def cap_candidates(candidates: list[ResearchCandidateCreate], kind: str) -> tuple[list[ResearchCandidateCreate], int]:
    """De-duplicate by symbol (first occurrence wins — record_brief refuses
    outright on a repeat) and cap at the kind's limit. Returns (kept,
    dropped_count)."""
    limit = NIGHTLY_MAX_CANDIDATES if kind == RESEARCH_KIND_NIGHTLY else MONTHLY_MAX_CANDIDATES
    seen: set[str] = set()
    deduped: list[ResearchCandidateCreate] = []
    for c in candidates:
        if c.symbol in seen:
            continue
        seen.add(c.symbol)
        deduped.append(c)
    kept = deduped[:limit]
    dropped = len(deduped) - len(kept)
    return kept, dropped


def filter_priced(
    candidates: list[ResearchCandidateCreate], prices: dict[str, float]
) -> tuple[list[ResearchCandidateCreate], list[str]]:
    """Drop any candidate record_brief would otherwise refuse the WHOLE
    brief for (a symbol not priced in the snapshot). Returns (kept,
    dropped_symbols)."""
    kept = [c for c in candidates if c.symbol in prices]
    dropped = [c.symbol for c in candidates if c.symbol not in prices]
    return kept, dropped


# ---------------------------------------------------------------------------
# The LLM call (the operator's Claude Code CLI subscription, never the paid
# Anthropic API)
# ---------------------------------------------------------------------------


def resolve_cli_path() -> str | None:
    """BASIS_CLAUDE_CLI (.env) if set, else whatever `shutil.which("claude")`
    finds on PATH. None means unresolved — a Windows Scheduled Task's PATH
    is minimal, so a bare "claude" is not assumed to resolve there the way
    it does in an interactive shell."""
    override = (os.environ.get(CLI_VAR) or "").strip()
    if override:
        return override
    return shutil.which("claude")


@dataclass(frozen=True)
class BriefCompletion:
    """One successful model call: the validated output, plus the model id
    the CLI actually reports it ran (never just the id we asked for —
    that's the whole point of recording model_id at all)."""

    output: BriefOut
    model_id: str


class BriefLLM(Protocol):
    """Every call the brief runner makes to a model. Tests pass a fake —
    no test may reach the real CLI or a subprocess."""

    def complete(self, system: str, user: str) -> BriefCompletion: ...


class ClaudeCLILLM:
    """The pinned model via `claude -p`, billed against the operator's
    Claude Code subscription — never the paid Anthropic API (operator
    ruling, #1131). One subprocess per call: the prompt arrives on stdin,
    `--tools ""` and `--safe-mode` keep it to "read the prompt, answer" (no
    tool access, no project hooks/CLAUDE.md/skills), `--no-session-
    persistence` keeps the run out of the operator's `~/.claude/projects`,
    and `--json-schema` has the CLI enforce BriefOut's shape itself. The
    reply is still re-validated here with `BriefOut.model_validate(...,
    strict=True)` — a schema-shaped reply from an external process is an
    untrusted input until that passes, same as any other boundary.

    Every failure mode (the executable can't be resolved or run, a nonzero
    exit, a timeout, stdout that isn't JSON, a CLI-reported failure, a
    reply that fails schema validation) raises ResearchBriefError so the
    caller can fail closed: nothing gets recorded."""

    def __init__(
        self,
        cli_path: str,
        *,
        model: str = MODEL_ID,
        timeout: float = NIGHTLY_CLI_TIMEOUT_SECONDS,
        run=subprocess.run,
    ):
        self._cli = cli_path
        self._model = model
        self._timeout = timeout
        self._run = run  # injected for tests; production passes subprocess.run

    def complete(self, system: str, user: str) -> BriefCompletion:
        argv = [
            self._cli,
            "-p",
            "--model",
            self._model,
            "--output-format",
            "json",
            "--tools",
            "",
            "--safe-mode",
            "--no-session-persistence",
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(BriefOut.model_json_schema()),
        ]
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}

        try:
            result = self._run(
                argv,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ResearchBriefError(f"the claude CLI ({self._cli!r}) could not be run: {exc}") from exc
        except OSError as exc:
            raise ResearchBriefError(f"the claude CLI ({self._cli!r}) could not be run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResearchBriefError(f"the claude CLI timed out after {self._timeout}s") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise ResearchBriefError(f"the claude CLI exited {result.returncode}: {detail}")

        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise ResearchBriefError(f"the claude CLI's stdout did not parse as JSON: {exc}") from exc

        if payload.get("is_error") or payload.get("subtype") != "success":
            raise ResearchBriefError(
                "the claude CLI reported a failed run "
                f"(subtype={payload.get('subtype')!r}, api_error_status={payload.get('api_error_status')!r})"
            )

        structured = payload.get("structured_output")
        if structured is None:
            raise ResearchBriefError("the claude CLI's reply had no structured_output to validate")

        try:
            output = BriefOut.model_validate(structured, strict=True)
        except ValidationError as exc:
            raise ResearchBriefError(f"the claude CLI's reply failed schema validation: {exc}") from exc

        model_used = next(iter((payload.get("modelUsage") or {}).keys()), None) or self._model
        return BriefCompletion(output=output, model_id=model_used)


# ---------------------------------------------------------------------------
# Finding the snapshot to brief, and what the held picks' theses were
# ---------------------------------------------------------------------------


async def latest_complete_snapshot(session: AsyncSession, kind: str) -> ResearchSnapshotModel | None:
    """The newest COMPLETE snapshot of *kind*, or None."""
    rows = (
        (
            await session.execute(
                select(ResearchSnapshotModel)
                .filter_by(kind=kind, status=SNAPSHOT_COMPLETE_STATUS)
                .order_by(ResearchSnapshotModel.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


async def latest_snapshot(session: AsyncSession, kind: str) -> ResearchSnapshotModel | None:
    """The newest snapshot of *kind* regardless of status — used only to
    explain a skip (an INCOMPLETE night should say why, not just "no
    snapshot")."""
    return (
        (
            await session.execute(
                select(ResearchSnapshotModel).filter_by(kind=kind).order_by(ResearchSnapshotModel.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


async def has_brief(session: AsyncSession, snapshot_id: str) -> bool:
    row = (await session.execute(select(ResearchBriefModel.id).filter_by(snapshot_id=snapshot_id))).first()
    return row is not None


async def held_pick_theses(session: AsyncSession) -> dict[str, dict[str, str]]:
    """symbol -> {thesis, risks, proves_wrong} from the most recent PICK
    decision on that symbol. The nightly brief's whole job is checking these
    against tonight's snapshot, so it needs the ORIGINAL text, not just the
    ticker (held.json, which the snapshot carries, has only the symbol)."""
    rows = (
        await session.execute(
            select(
                OperatorPickModel.id,
                OperatorPickModel.symbol,
                ResearchCandidateModel.thesis,
                ResearchCandidateModel.risks,
                ResearchCandidateModel.proves_wrong,
            )
            .join(ResearchCandidateModel, ResearchCandidateModel.id == OperatorPickModel.candidate_id)
            .filter(OperatorPickModel.decision == PICK_DECISION_PICK)
            .order_by(OperatorPickModel.id)
        )
    ).all()
    out: dict[str, dict[str, str]] = {}
    for _id, symbol, thesis, risks, proves_wrong in rows:
        out[symbol] = {"thesis": thesis, "risks": risks, "proves_wrong": proves_wrong}
    return out


async def _skip_reason(
    session: AsyncSession, kind: str, already_briefed: ResearchSnapshotModel | None, today: date | None
) -> tuple[str, bool]:
    """(reason, alert). ALWAYS checks the newest snapshot of *kind*
    regardless of status first — an already-briefed COMPLETE snapshot from
    a prior night must never mask tonight's INCOMPLETE one, or a crash that
    wrote no row at all, behind a reassuring "already has a brief"."""
    latest = await latest_snapshot(session, kind)
    if latest is None:
        return f"no {kind} snapshot recorded yet", True
    if latest.status != SNAPSHOT_COMPLETE_STATUS:
        why = "; ".join(latest.reasons or []) or "no reason recorded"
        return f"snapshot {latest.id} is {latest.status}: {why}", True
    if today is not None:
        try:
            latest_as_of = date.fromisoformat(latest.as_of)
        except ValueError:
            latest_as_of = None
        if latest_as_of is not None and latest_as_of < today:
            return f"no {kind} snapshot recorded today (latest is {latest.as_of})", True
    if already_briefed is not None:
        return f"snapshot {already_briefed.id} already has a brief", False
    return f"no {kind} snapshot to brief yet", False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefOutcome:
    recorded: bool
    kind: str
    reason: str | None = None
    alert: bool = False
    brief_id: int | None = None
    summary: str = ""
    candidate_symbols: tuple[str, ...] = ()
    dropped_unpriced: tuple[str, ...] = field(default_factory=tuple)
    dropped_over_cap: int = 0


async def run_research_brief(
    session_maker: async_sessionmaker[AsyncSession], llm: BriefLLM, *, kind: str, today: date | None = None
) -> BriefOutcome:
    """Find the snapshot, build the prompt, call the model, validate and
    record. A no-op (recorded=False) is not an error by itself — but
    BriefOutcome.alert tells the caller whether it's an ORDINARY no-op
    (already briefed) or a real problem (no snapshot, an INCOMPLETE one, or
    nothing fresher than *today*) that should push loudly. *today* (pass
    market_today()) enables the "nothing recorded today" check; omit it
    only when the caller cannot know the market date."""
    from backend.research import record_brief
    from backend.research_snapshot import SnapshotIntegrityError, load_snapshot_prices

    if kind not in RESEARCH_KINDS:
        raise ResearchBriefError(f"unknown brief kind {kind!r}")

    async with session_maker() as session:
        snapshot = await latest_complete_snapshot(session, kind)
        already_briefed = snapshot is not None and await has_brief(session, snapshot.id)
        if snapshot is None or already_briefed:
            reason, alert = await _skip_reason(session, kind, snapshot if already_briefed else None, today)
            return BriefOutcome(recorded=False, kind=kind, reason=reason, alert=alert)
        theses = await held_pick_theses(session)

    try:
        prices = load_snapshot_prices(snapshot.path, snapshot.content_hash)
    except SnapshotIntegrityError as exc:
        raise ResearchBriefError(f"snapshot {snapshot.id} failed its integrity check: {exc}") from exc

    digest = render_snapshot_digest(Path(snapshot.path), held_theses=theses)
    user_prompt = f"{frozen_instructions(kind)}\n\n{digest}"
    try:
        completion = llm.complete(SYSTEM_PROMPT, user_prompt)
    except ResearchBriefError as exc:
        # The CLI call itself failed (not resolved, not logged in, a
        # nonzero exit, a timeout, an unparseable/invalid reply) — fail
        # closed exactly like any other skip: nothing recorded, and it
        # surfaces the same way (BriefOutcome.alert -> a "high" push).
        return BriefOutcome(recorded=False, kind=kind, reason=f"the model call failed: {exc}", alert=True)

    brief_out = completion.output
    summary = brief_out.summary.strip()
    if not summary:
        raise ResearchBriefError("the model's summary was empty")
    candidates = to_candidates(brief_out.candidates)
    capped, dropped_over_cap = cap_candidates(candidates, kind)
    kept, dropped_unpriced = filter_priced(capped, prices)

    if dropped_unpriced:
        summary = f"{summary} (dropped unpriced: {', '.join(dropped_unpriced)})"
    if dropped_over_cap:
        summary = f"{summary} (dropped {dropped_over_cap} candidate(s) over the per-brief cap)"

    create = ResearchBriefCreate(
        snapshot_id=snapshot.id,
        kind=kind,
        model_id=completion.model_id,
        prompt_hash=prompt_hash_for(kind),
        summary=summary,
        candidates=kept,
    )
    async with session_maker() as session:
        brief = await record_brief(session, create)
    return BriefOutcome(
        recorded=True,
        kind=kind,
        brief_id=brief.id,
        summary=summary,
        candidate_symbols=tuple(c.symbol for c in kept),
        dropped_unpriced=tuple(dropped_unpriced),
        dropped_over_cap=dropped_over_cap,
    )


def compose_brief_push(outcome: BriefOutcome) -> tuple[str, str]:
    """(title, body) for the ntfy push — ALWAYS sent (even on a skip): a
    quiet night that never ran must not look like a quiet night that did."""
    label = outcome.kind.lower()
    if not outcome.recorded:
        return f"basis research brief ({label}): skipped", outcome.reason or "skipped"
    title = f"basis research brief ({label}): {len(outcome.candidate_symbols)} new candidate(s)"
    lines = [outcome.summary]
    if outcome.candidate_symbols:
        lines.append("")
        lines.append("New: " + ", ".join(outcome.candidate_symbols))
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# The monthly "is today due" check (no Task-Scheduler-native equivalent)
# ---------------------------------------------------------------------------


async def monthly_brief_due(session: AsyncSession, today: date) -> bool:
    """True when today is a trading day AND no MONTHLY BRIEF exists whose
    snapshot's as_of falls in this calendar month. Checking for a BRIEF
    (not just a COMPLETE snapshot) on purpose: a COMPLETE day-1 snapshot
    whose brief step then crashed (model API outage, a bad key) must still
    retry the next trading day — gating on the snapshot alone would read
    that as "done" and silently skip the rest of the month. The monthly
    task is registered on the same every-weekday trigger as nightly
    (Task Scheduler has no native monthly trigger); this check is what
    makes a failure on any weekday self-correcting on the next one —
    "a missed slot runs at the next opportunity" (spec/research-brief.md)."""
    from backend.calendars import is_trading_day

    if not is_trading_day(today):
        return False
    rows = (
        (
            await session.execute(
                select(ResearchSnapshotModel.as_of)
                .join(ResearchBriefModel, ResearchBriefModel.snapshot_id == ResearchSnapshotModel.id)
                .filter(ResearchSnapshotModel.kind == RESEARCH_KIND_MONTHLY)
            )
        )
        .scalars()
        .all()
    )
    for as_of in rows:
        d = date.fromisoformat(as_of)
        if (d.year, d.month) == (today.year, today.month):
            return False
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pixi run research-brief",
        description="Write a research brief from the latest unbriefed COMPLETE snapshot (#1131).",
    )
    parser.add_argument("--kind", choices=("nightly", "monthly"), default="nightly")
    parser.add_argument(
        "--check-due",
        action="store_true",
        help="exit 0 if this kind should run today and 1 otherwise; makes no writes",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    kind = args.kind.upper()

    if args.check_due and kind == RESEARCH_KIND_NIGHTLY:
        print("research-brief --check-due nightly: due")
        return 0

    from backend.env import load_env

    try:
        load_env()
    except RuntimeError as exc:
        print(f"research-brief NOT RUN: {exc}", file=sys.stderr)
        return 2
    from backend.database import TRADING_MODE, async_session_maker, init_db

    if TRADING_MODE != "live":
        print(
            "research-brief NOT RUN: research records live in the live database — run it with the live overlay",
            file=sys.stderr,
        )
        return 2

    if args.check_due:
        from backend.dates import market_today

        async def _check() -> bool:
            await init_db()
            async with async_session_maker() as session:
                return await monthly_brief_due(session, market_today())

        due = asyncio.run(_check())
        print(f"research-brief --check-due monthly: {'due' if due else 'not due'}")
        return 0 if due else 1

    from backend.operator import alert_crash, send_ntfy

    cli_path = resolve_cli_path()
    if not cli_path:
        message = f"the claude CLI could not be resolved (set {CLI_VAR} in .env, or put claude on PATH)"
        print(f"research-brief NOT RUN: {message}", file=sys.stderr)
        send_ntfy("basis research brief NOT RUN", message, "high")
        return 2

    from backend.dates import market_today

    async def _run() -> BriefOutcome:
        await init_db()
        llm = ClaudeCLILLM(cli_path, timeout=CLI_TIMEOUT_BY_KIND[kind])
        return await run_research_brief(async_session_maker, llm, kind=kind, today=market_today())

    try:
        outcome = asyncio.run(_run())
    except Exception as exc:
        logger.exception("research-brief (%s) crashed", args.kind)
        alert_crash("basis research brief CRASHED", f"{kind}: {type(exc).__name__}: {exc}", "high")
        return 4

    title, body = compose_brief_push(outcome)
    print(f"{title}\n{body}")
    # Never silent: a skip (no snapshot, INCOMPLETE, nothing fresher than
    # today) pushes too, at "high" whenever BriefOutcome.alert says it's a
    # real problem rather than an ordinary already-briefed no-op.
    send_ntfy(title, body, "high" if outcome.alert else "default")
    return 0


if __name__ == "__main__":
    sys.exit(main())
