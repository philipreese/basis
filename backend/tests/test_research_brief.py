"""#1131 phase 2: the research brief runner. No network — BriefLLM is a
Protocol and every test passes a fake; AnthropicLLM itself is exercised
against a fake anthropic client object, never the real SDK/network."""

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import research
from backend import research_brief as rb
from backend.models import OperatorPickRequest, ResearchCandidateCreate, TradingControlModel
from backend.research_snapshot import MANIFEST, content_hash_of, sha256_file
from backend.seeds import PICKS_BOOK_ID
from backend.states import PICK_DECISION_PASS, PICK_DECISION_PICK, RESEARCH_KIND_MONTHLY, RESEARCH_KIND_NIGHTLY
from backend.trading_control import ACTIVE

CAP_VAR = "BASIS_MANUAL_CAP_P01"


async def _resume_p01(m) -> None:
    async with m() as session:
        await session.execute(
            update(TradingControlModel).where(TradingControlModel.scope == PICKS_BOOK_ID).values(state=ACTIVE)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# A full snapshot folder (universe/held/filings/prices), for render +
# integration tests
# ---------------------------------------------------------------------------


def _write_snapshot_folder(
    root: Path,
    *,
    snapshot_id: str = "20261005T213000Z",
    prices: dict[str, float] | None = None,
    held: list[str] | None = None,
    n_universe: int = 3,
    n_filings: int = 1,
) -> tuple[str, str]:
    prices = prices or {"ABCD": 20.0, "WXYZ": 50.0}
    folder = root / snapshot_id
    (folder / "filings").mkdir(parents=True)
    members = [
        {
            "symbol": s,
            "name": f"{s} Inc",
            "cik": "0000000001",
            "market_cap": 1_000_000_000.0,
            "last_sale": p,
            "volume": 100_000.0,
            "dollar_volume": p * 100_000.0,
            "sector": "Industrials",
            "industry": "Machinery",
        }
        for s, p in list(prices.items())[:n_universe]
    ]
    (folder / "universe.json").write_text(json.dumps({"members": members, "excluded_no_cik": []}), encoding="utf-8")
    (folder / "held.json").write_text(json.dumps({"symbols": held or []}), encoding="utf-8")

    filing_rows = []
    for i, s in enumerate(list(prices)[:n_filings]):
        excerpt_name = f"filings/{s}_8-K_000{i}.txt"
        (folder / excerpt_name).write_text(f"{s} press release text.", encoding="utf-8")
        filing_rows.append(
            {
                "symbol": s,
                "cik": "0000000001",
                "form": "8-K",
                "accession": f"000{i}",
                "filing_date": "2026-10-01",
                "accepted_at": "2026-10-01T20:00:00+00:00",
                "url": "https://example.invalid",
                "excerpt_file": excerpt_name,
            }
        )
    (folder / "filings.json").write_text(json.dumps({"since": "2026-10-01", "filings": filing_rows}), encoding="utf-8")
    (folder / "prices.json").write_text(
        json.dumps(
            {
                s: {
                    "close": p,
                    "source": "screener",
                    "history": [["2026-07-01", p * 0.9, 1000.0], ["2026-10-01", p, 1000.0]],
                }
                for s, p in prices.items()
            }
        ),
        encoding="utf-8",
    )

    files = {
        p.relative_to(folder).as_posix(): sha256_file(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    }
    digest = content_hash_of(files)
    (folder / MANIFEST).write_text(json.dumps({"files": files, "content_hash": digest}), encoding="utf-8")
    return str(folder), digest


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_stable_and_distinct_per_kind(self):
        h1 = rb.prompt_hash_for(RESEARCH_KIND_NIGHTLY)
        h2 = rb.prompt_hash_for(RESEARCH_KIND_NIGHTLY)
        h3 = rb.prompt_hash_for(RESEARCH_KIND_MONTHLY)
        assert h1 == h2
        assert h1 != h3

    def test_unknown_kind_refused(self):
        with pytest.raises(rb.ResearchBriefError):
            rb.frozen_instructions("WEEKLY")
        with pytest.raises(rb.ResearchBriefError):
            rb.prompt_hash_for("WEEKLY")


class TestRenderSnapshotDigest:
    def test_includes_held_thesis_price_and_filing_excerpt(self, tmp_path):
        path, _ = _write_snapshot_folder(tmp_path, held=["ABCD"])
        theses = {"ABCD": {"thesis": "margin expansion", "risks": "competition", "proves_wrong": "margin falls"}}
        digest = rb.render_snapshot_digest(Path(path), held_theses=theses)
        assert "Held picks: ABCD" in digest
        assert "margin expansion" in digest
        assert "close 20.00" in digest
        assert "WXYZ" in digest
        assert "press release text" in digest

    def test_held_pick_without_a_recorded_thesis_says_so(self, tmp_path):
        path, _ = _write_snapshot_folder(tmp_path, held=["ABCD"])
        digest = rb.render_snapshot_digest(Path(path))
        assert "no recorded thesis" in digest

    def test_no_held_picks_reads_none(self, tmp_path):
        path, _ = _write_snapshot_folder(tmp_path)
        digest = rb.render_snapshot_digest(Path(path))
        assert "Held picks: (none)" in digest

    def test_universe_is_rendered_in_full_by_default(self, tmp_path):
        path, _ = _write_snapshot_folder(
            tmp_path, prices={f"S{i}": float(i + 1) for i in range(5)}, n_universe=5, n_filings=0
        )
        digest = rb.render_snapshot_digest(Path(path))
        assert digest.count(" Inc |") == 5

    def test_filings_are_bounded_and_held_picks_come_first(self, tmp_path):
        path, _ = _write_snapshot_folder(
            tmp_path, prices={f"S{i}": float(i + 1) for i in range(5)}, held=["S4"], n_universe=5, n_filings=5
        )
        digest = rb.render_snapshot_digest(Path(path), max_filings=1)
        assert digest.count(" filed ") == 1
        assert "S4 8-K filed" in digest

    def test_missing_optional_files_read_as_empty(self, tmp_path):
        folder = tmp_path / "bare"
        folder.mkdir()
        digest = rb.render_snapshot_digest(folder)
        assert "Held picks: (none)" in digest
        assert "Universe: 0 name(s)" in digest


class TestToCandidates:
    def test_valid_items(self):
        out = rb.to_candidates([rb.BriefCandidateOut(symbol="abcd", thesis="t", risks="r", proves_wrong="p")])
        assert out == [ResearchCandidateCreate(symbol="ABCD", thesis="t", risks="r", proves_wrong="p")]

    def test_normalizing_to_a_blank_symbol_is_refused(self):
        # BriefCandidateOut accepts a whitespace-only symbol (min_length=1
        # is satisfied pre-strip); normalizing it strips to empty, which
        # ResearchCandidateCreate refuses — a real distinct failure mode.
        with pytest.raises(rb.ResearchBriefError):
            rb.to_candidates([rb.BriefCandidateOut(symbol=" ", thesis="t", risks="r", proves_wrong="p")])


class TestCapCandidates:
    def _c(self, symbol):
        return ResearchCandidateCreate(symbol=symbol, thesis="t", risks="r", proves_wrong="p")

    def test_nightly_caps_at_two(self):
        kept, dropped = rb.cap_candidates([self._c(s) for s in ("A", "B", "C")], RESEARCH_KIND_NIGHTLY)
        assert [c.symbol for c in kept] == ["A", "B"]
        assert dropped == 1

    def test_dedupes_by_symbol_first_occurrence_wins(self):
        kept, dropped = rb.cap_candidates([self._c("A"), self._c("A")], RESEARCH_KIND_NIGHTLY)
        assert [c.symbol for c in kept] == ["A"]
        assert dropped == 0

    def test_monthly_cap_is_higher(self):
        kept, dropped = rb.cap_candidates(
            [self._c(f"S{i}") for i in range(rb.MONTHLY_MAX_CANDIDATES + 2)], RESEARCH_KIND_MONTHLY
        )
        assert len(kept) == rb.MONTHLY_MAX_CANDIDATES
        assert dropped == 2


class TestFilterPriced:
    def test_drops_unpriced_symbols(self):
        c_ok = ResearchCandidateCreate(symbol="ABCD", thesis="t", risks="r", proves_wrong="p")
        c_bad = ResearchCandidateCreate(symbol="ZZZZ", thesis="t", risks="r", proves_wrong="p")
        kept, dropped = rb.filter_priced([c_ok, c_bad], {"ABCD": 20.0})
        assert [c.symbol for c in kept] == ["ABCD"]
        assert dropped == ["ZZZZ"]


class TestComposeBriefPush:
    def test_skipped_outcome(self):
        outcome = rb.BriefOutcome(recorded=False, kind="NIGHTLY", reason="no snapshot yet")
        title, body = rb.compose_brief_push(outcome)
        assert "skipped" in title
        assert body == "no snapshot yet"

    def test_recorded_outcome_lists_candidates(self):
        outcome = rb.BriefOutcome(
            recorded=True, kind="NIGHTLY", brief_id=1, summary="two new", candidate_symbols=("ABCD", "WXYZ")
        )
        title, body = rb.compose_brief_push(outcome)
        assert "2 new candidate" in title
        assert "ABCD" in body and "WXYZ" in body


# ---------------------------------------------------------------------------
# AnthropicLLM (a fake SDK client object — never the real network)
# ---------------------------------------------------------------------------


class _FakeStopDetails:
    def __init__(self, category):
        self.category = category


class _FakeResponse:
    def __init__(self, parsed_output=None, stop_reason="end_turn", stop_details=None):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class TestAnthropicLLM:
    def test_returns_parsed_output_and_passes_through_the_call(self):
        out = rb.BriefOut(summary="ok", candidates=[])
        client = _FakeAnthropicClient(_FakeResponse(parsed_output=out))
        llm = rb.AnthropicLLM("secret-key", client=client)
        result = llm.complete("sys", "user", max_tokens=2048)
        assert result is out
        assert client.messages.last_kwargs["model"] == rb.MODEL_ID
        assert client.messages.last_kwargs["system"] == "sys"
        assert client.messages.last_kwargs["max_tokens"] == 2048
        assert client.messages.last_kwargs["output_format"] is rb.BriefOut

    def test_truncated_reply_is_refused(self):
        client = _FakeAnthropicClient(_FakeResponse(parsed_output=None, stop_reason="max_tokens"))
        llm = rb.AnthropicLLM("secret-key", client=client)
        with pytest.raises(rb.ResearchBriefError):
            llm.complete("sys", "user", max_tokens=16)

    def test_no_parsed_output_is_refused(self):
        client = _FakeAnthropicClient(_FakeResponse(parsed_output=None, stop_reason="end_turn"))
        llm = rb.AnthropicLLM("secret-key", client=client)
        with pytest.raises(rb.ResearchBriefError):
            llm.complete("sys", "user", max_tokens=16)

    def test_a_refusal_is_refused_not_misread_as_a_schema_failure(self):
        client = _FakeAnthropicClient(
            _FakeResponse(parsed_output=None, stop_reason="refusal", stop_details=_FakeStopDetails("cyber"))
        )
        llm = rb.AnthropicLLM("secret-key", client=client)
        with pytest.raises(rb.ResearchBriefError, match="refused"):
            llm.complete("sys", "user", max_tokens=16)


# ---------------------------------------------------------------------------
# run_research_brief: full orchestration against a real (sandboxed) sqlite db
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, summary: str, candidates: list[dict] | None = None):
        self._out = rb.BriefOut(summary=summary, candidates=[rb.BriefCandidateOut(**c) for c in (candidates or [])])

    def complete(self, system: str, user: str, *, max_tokens: int) -> rb.BriefOut:
        return self._out


@pytest.fixture
def maker(tmp_path, monkeypatch):
    import backend.database as db_mod

    url = f"sqlite+aiosqlite:///{(tmp_path / 'brief.db').as_posix()}"
    monkeypatch.setattr(db_mod, "DATABASE_URL", url)
    engine = create_async_engine(url)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(db_mod, "async_session_maker", m)
    monkeypatch.setattr(db_mod, "TRADING_MODE", "live")
    monkeypatch.delenv(CAP_VAR, raising=False)
    return db_mod, m


async def _seed(maker_pair):
    db_mod, m = maker_pair
    await db_mod.init_db()
    return m


async def _record_snapshot(
    m,
    path,
    digest,
    *,
    kind="NIGHTLY",
    snapshot_id="20261005T213000Z",
    as_of="2026-10-05",
    created_at="2026-10-05T21:30:00+00:00",
):
    async with m() as session:
        await research.record_snapshot(
            session,
            snapshot_id=snapshot_id,
            kind=kind,
            as_of=as_of,
            created_at=created_at,
            path=path,
            content_hash=digest,
            status="COMPLETE",
            reasons=[],
            counts={"universe": 2},
        )


async def _record_incomplete_snapshot(m, *, kind="NIGHTLY", snapshot_id="20261004T213000Z"):
    async with m() as session:
        await research.record_snapshot(
            session,
            snapshot_id=snapshot_id,
            kind=kind,
            as_of="2026-10-04",
            created_at="2026-10-04T21:30:00+00:00",
            path="",
            content_hash="",
            status="INCOMPLETE",
            reasons=["prices: 2 of 3 failed"],
            counts={},
        )


class TestRunResearchBrief:
    @pytest.mark.asyncio
    async def test_no_snapshot_at_all_is_a_clean_no_op_but_still_alerts(self, maker):
        m = await _seed(maker)
        outcome = await rb.run_research_brief(m, FakeLLM("x"), kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is False
        assert "no NIGHTLY snapshot" in outcome.reason
        assert outcome.alert is True

    @pytest.mark.asyncio
    async def test_incomplete_snapshot_surfaces_its_reasons_and_alerts(self, maker):
        m = await _seed(maker)
        await _record_incomplete_snapshot(m)
        outcome = await rb.run_research_brief(m, FakeLLM("x"), kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is False
        assert "INCOMPLETE" in outcome.reason
        assert "prices: 2 of 3 failed" in outcome.reason
        assert outcome.alert is True

    @pytest.mark.asyncio
    async def test_already_briefed_snapshot_is_skipped_without_alerting(self, maker, tmp_path):
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path)
        await _record_snapshot(m, path, digest)
        first = await rb.run_research_brief(m, FakeLLM("nothing new", []), kind=RESEARCH_KIND_NIGHTLY)
        assert first.recorded is True
        second = await rb.run_research_brief(m, FakeLLM("should not be read"), kind=RESEARCH_KIND_NIGHTLY)
        assert second.recorded is False
        assert "already has a brief" in second.reason
        assert second.alert is False

    @pytest.mark.asyncio
    async def test_a_fresh_incomplete_snapshot_is_not_masked_by_an_older_briefed_one(self, maker, tmp_path):
        """Night two: last night's COMPLETE snapshot was already briefed;
        tonight's run wrote a fresh INCOMPLETE one. The skip must surface
        TONIGHT's failure, not read as an ordinary already-briefed no-op."""
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path, snapshot_id="20261004T213000Z")
        await _record_snapshot(
            m,
            path,
            digest,
            snapshot_id="20261004T213000Z",
            as_of="2026-10-04",
            created_at="2026-10-04T21:30:00+00:00",
        )
        first = await rb.run_research_brief(m, FakeLLM("nothing new", []), kind=RESEARCH_KIND_NIGHTLY)
        assert first.recorded is True
        async with m() as session:
            await research.record_snapshot(
                session,
                snapshot_id="20261005T213000Z",
                kind="NIGHTLY",
                as_of="2026-10-05",
                created_at="2026-10-05T21:30:00+00:00",
                path="",
                content_hash="",
                status="INCOMPLETE",
                reasons=["universe: the screen kept no names"],
                counts={},
            )
        outcome = await rb.run_research_brief(m, FakeLLM("should not be read"), kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is False
        assert outcome.alert is True
        assert "20261005T213000Z" in outcome.reason
        assert "universe: the screen kept no names" in outcome.reason

    @pytest.mark.asyncio
    async def test_nothing_recorded_today_is_not_masked_by_an_older_briefed_snapshot(self, maker, tmp_path):
        """A crash so early that no snapshot row is written at all tonight:
        the newest row is still last night's (already-briefed) COMPLETE
        one. today= lets the skip say "nothing today" instead of a
        reassuring "already has a brief"."""
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path, snapshot_id="20261004T213000Z")
        await _record_snapshot(
            m,
            path,
            digest,
            snapshot_id="20261004T213000Z",
            as_of="2026-10-04",
            created_at="2026-10-04T21:30:00+00:00",
        )
        first = await rb.run_research_brief(m, FakeLLM("nothing new", []), kind=RESEARCH_KIND_NIGHTLY)
        assert first.recorded is True
        outcome = await rb.run_research_brief(
            m, FakeLLM("should not be read"), kind=RESEARCH_KIND_NIGHTLY, today=date(2026, 10, 5)
        )
        assert outcome.recorded is False
        assert outcome.alert is True
        assert "no NIGHTLY snapshot recorded today" in outcome.reason

    @pytest.mark.asyncio
    async def test_drops_an_unpriced_candidate_without_failing_the_brief(self, maker, tmp_path):
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path, prices={"ABCD": 20.0, "WXYZ": 50.0})
        await _record_snapshot(m, path, digest)
        llm = FakeLLM(
            "two candidates",
            [
                {"symbol": "ABCD", "thesis": "t", "risks": "r", "proves_wrong": "p"},
                {"symbol": "ZZZZ", "thesis": "t", "risks": "r", "proves_wrong": "p"},  # unpriced
            ],
        )
        outcome = await rb.run_research_brief(m, llm, kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is True
        assert outcome.candidate_symbols == ("ABCD",)
        assert outcome.dropped_unpriced == ("ZZZZ",)
        assert outcome.dropped_over_cap == 0
        assert "ZZZZ" in outcome.summary

    @pytest.mark.asyncio
    async def test_caps_before_filtering_so_a_dropped_slot_is_not_backfilled(self, maker, tmp_path):
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path, prices={"ABCD": 20.0, "WXYZ": 50.0, "QRST": 10.0})
        await _record_snapshot(m, path, digest)
        llm = FakeLLM(
            "three",
            [
                {"symbol": "ZZZZ", "thesis": "t", "risks": "r", "proves_wrong": "p"},  # unpriced, cap slot 1
                {"symbol": "ABCD", "thesis": "t", "risks": "r", "proves_wrong": "p"},  # cap slot 2
                {"symbol": "WXYZ", "thesis": "t", "risks": "r", "proves_wrong": "p"},  # dropped by the nightly cap
            ],
        )
        outcome = await rb.run_research_brief(m, llm, kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.candidate_symbols == ("ABCD",)
        assert outcome.dropped_unpriced == ("ZZZZ",)
        assert outcome.dropped_over_cap == 1

    @pytest.mark.asyncio
    async def test_held_picks_thesis_reaches_the_prompt(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        monkeypatch.setenv(CAP_VAR, "1000")
        await _resume_p01(m)
        path, digest = _write_snapshot_folder(tmp_path, prices={"ABCD": 20.0}, held=["ABCD"])
        await _record_snapshot(m, path, digest)
        candidate_id = await _seed_one_candidate(m)
        async with m() as session:
            await research.record_pick(
                session, OperatorPickRequest(candidate_id=candidate_id, decision=PICK_DECISION_PICK)
            )
        captured = {}

        class _CapturingLLM:
            def complete(self, system, user, *, max_tokens):
                captured["user"] = user
                return rb.BriefOut(summary="held pick still intact", candidates=[])

        outcome = await rb.run_research_brief(m, _CapturingLLM(), kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is True
        assert "original thesis text" in captured["user"]

    @pytest.mark.asyncio
    async def test_empty_model_summary_raises(self, maker, tmp_path):
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path)
        await _record_snapshot(m, path, digest)
        with pytest.raises(rb.ResearchBriefError):
            await rb.run_research_brief(m, FakeLLM("   "), kind=RESEARCH_KIND_NIGHTLY)

    @pytest.mark.asyncio
    async def test_snapshot_integrity_failure_raises(self, maker, tmp_path):
        m = await _seed(maker)
        path, digest = _write_snapshot_folder(tmp_path)
        await _record_snapshot(m, path, digest)
        (Path(path) / "prices.json").write_text("{}", encoding="utf-8")  # mutate after recording
        with pytest.raises(rb.ResearchBriefError):
            await rb.run_research_brief(m, FakeLLM("x"), kind=RESEARCH_KIND_NIGHTLY)

    @pytest.mark.asyncio
    async def test_picks_the_latest_complete_snapshot_of_the_requested_kind(self, maker, tmp_path):
        m = await _seed(maker)
        older_path, older_digest = _write_snapshot_folder(tmp_path / "older", snapshot_id="20261001T213000Z")
        newer_path, newer_digest = _write_snapshot_folder(tmp_path / "newer", snapshot_id="20261005T213000Z")
        async with m() as session:
            await research.record_snapshot(
                session,
                snapshot_id="20261001T213000Z",
                kind="NIGHTLY",
                as_of="2026-10-01",
                created_at="2026-10-01T21:30:00+00:00",
                path=older_path,
                content_hash=older_digest,
                status="COMPLETE",
                reasons=[],
                counts={},
            )
        await _record_snapshot(m, newer_path, newer_digest, snapshot_id="20261005T213000Z")
        outcome = await rb.run_research_brief(m, FakeLLM("latest", []), kind=RESEARCH_KIND_NIGHTLY)
        assert outcome.recorded is True
        assert outcome.summary == "latest"

    @pytest.mark.asyncio
    async def test_unknown_kind_refused(self, maker):
        m = await _seed(maker)
        with pytest.raises(rb.ResearchBriefError):
            await rb.run_research_brief(m, FakeLLM("x"), kind="WEEKLY")


async def _seed_one_candidate(m) -> int:
    """Writes a COMPLETE snapshot-free brief/candidate directly so a PICK
    can be recorded against it. Returns the candidate id."""

    async with m() as session:
        await research.record_snapshot(
            session,
            snapshot_id="seed-snap",
            kind="NIGHTLY",
            as_of="2026-09-01",
            created_at="2026-09-01T21:30:00+00:00",
            path="unused",
            content_hash="unused",
            status="COMPLETE",
            reasons=[],
            counts={},
        )
    # record_brief re-verifies the snapshot hash, which "seed-snap" can't
    # pass — write the candidate row directly instead, bypassing record_brief.
    from backend.models import ResearchBriefModel, ResearchCandidateModel

    async with m() as session:
        brief = ResearchBriefModel(
            snapshot_id="seed-snap", kind="NIGHTLY", model_id="seed", prompt_hash=None, summary="seed", created_at="x"
        )
        session.add(brief)
        await session.flush()
        candidate = ResearchCandidateModel(
            brief_id=brief.id,
            symbol="ABCD",
            thesis="original thesis text",
            risks="original risks text",
            proves_wrong="original proves-wrong text",
            snapshot_price=20.0,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


class TestHeldPickTheses:
    @pytest.mark.asyncio
    async def test_only_picks_count_not_passes(self, maker, monkeypatch):
        m = await _seed(maker)
        monkeypatch.setenv(CAP_VAR, "1000")
        await _resume_p01(m)
        picked_id = await _seed_one_candidate(m)
        async with m() as session:
            await research.record_pick(
                session, OperatorPickRequest(candidate_id=picked_id, decision=PICK_DECISION_PICK)
            )
        async with m() as session:
            theses = await rb.held_pick_theses(session)
        assert theses["ABCD"]["thesis"] == "original thesis text"

    @pytest.mark.asyncio
    async def test_a_pass_is_not_a_held_pick(self, maker):
        m = await _seed(maker)
        candidate_id = await _seed_one_candidate(m)
        async with m() as session:
            await research.record_pick(
                session, OperatorPickRequest(candidate_id=candidate_id, decision=PICK_DECISION_PASS)
            )
        async with m() as session:
            theses = await rb.held_pick_theses(session)
        assert theses == {}


# ---------------------------------------------------------------------------
# monthly_brief_due
# ---------------------------------------------------------------------------


class TestMonthlyBriefDue:
    @pytest.mark.asyncio
    async def test_due_with_no_prior_monthly_snapshot_this_month(self, maker, monkeypatch):
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 2))  # Monday
        assert due is True

    @pytest.mark.asyncio
    async def test_not_due_on_a_non_trading_day(self, maker, monkeypatch):
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 1))  # Sunday
        assert due is False

    @pytest.mark.asyncio
    async def test_not_due_once_a_monthly_brief_exists_this_month(self, maker, monkeypatch, tmp_path):
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        path, digest = _write_snapshot_folder(tmp_path, snapshot_id="20261102T213000Z")
        await _record_snapshot(
            m,
            path,
            digest,
            kind=RESEARCH_KIND_MONTHLY,
            snapshot_id="20261102T213000Z",
            as_of="2026-11-02",
            created_at="2026-11-02T21:30:00+00:00",
        )
        recorded = await rb.run_research_brief(m, FakeLLM("full screen, nothing new", []), kind=RESEARCH_KIND_MONTHLY)
        assert recorded.recorded is True
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 3))  # Tuesday, later that month
        assert due is False

    @pytest.mark.asyncio
    async def test_still_due_after_an_incomplete_monthly_snapshot(self, maker, monkeypatch):
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        await _record_incomplete_snapshot(m, kind=RESEARCH_KIND_MONTHLY, snapshot_id="20261102T213000Z")
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 3))
        assert due is True

    @pytest.mark.asyncio
    async def test_still_due_when_the_snapshot_completed_but_its_brief_never_ran(self, maker, monkeypatch, tmp_path):
        """Day 1: the snapshot completed, but the brief step crashed before
        writing a row (a model API outage, a bad key). Gating on the
        snapshot alone would read this as "done" for the month; gating on
        a BRIEF existing must still say due on day 2."""
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        path, digest = _write_snapshot_folder(tmp_path, snapshot_id="20261102T213000Z")
        await _record_snapshot(
            m,
            path,
            digest,
            kind=RESEARCH_KIND_MONTHLY,
            snapshot_id="20261102T213000Z",
            as_of="2026-11-02",
            created_at="2026-11-02T21:30:00+00:00",
        )
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 3))
        assert due is True

    @pytest.mark.asyncio
    async def test_a_complete_snapshot_from_last_month_does_not_count(self, maker, monkeypatch, tmp_path):
        m = await _seed(maker)
        import backend.calendars as cal

        monkeypatch.setattr(cal, "is_trading_day", lambda d: d.weekday() < 5)
        path, digest = _write_snapshot_folder(tmp_path, snapshot_id="20261002T213000Z")
        await _record_snapshot(
            m,
            path,
            digest,
            kind=RESEARCH_KIND_MONTHLY,
            snapshot_id="20261002T213000Z",
            as_of="2026-10-02",
            created_at="2026-10-02T21:30:00+00:00",
        )
        recorded = await rb.run_research_brief(m, FakeLLM("last month's full screen", []), kind=RESEARCH_KIND_MONTHLY)
        assert recorded.recorded is True
        async with m() as session:
            due = await rb.monthly_brief_due(session, date(2026, 11, 2))
        assert due is True


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def test_check_due_nightly_is_always_due_with_no_env_needed(self, monkeypatch, capsys):
        monkeypatch.delenv("BASIS_ENV_OVERLAY", raising=False)
        assert rb.main(["--kind", "nightly", "--check-due"]) == 0
        assert "due" in capsys.readouterr().out

    def test_check_due_monthly_refuses_on_a_missing_overlay(self, monkeypatch, capsys):
        def missing():
            raise RuntimeError("overlay .env.live is missing")

        monkeypatch.setattr("backend.env.load_env", missing)
        assert rb.main(["--kind", "monthly", "--check-due"]) == 2
        assert "overlay" in capsys.readouterr().err

    def test_check_due_monthly_asks_the_database(self, monkeypatch, maker):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        asyncio.run(_seed(maker))
        monkeypatch.setattr(rb, "monthly_brief_due", lambda session, today: _async_true())
        assert rb.main(["--kind", "monthly", "--check-due"]) == 0
        monkeypatch.setattr(rb, "monthly_brief_due", lambda session, today: _async_false())
        assert rb.main(["--kind", "monthly", "--check-due"]) == 1

    def test_refuses_on_a_missing_overlay(self, monkeypatch, capsys):
        def missing():
            raise RuntimeError("overlay .env.live is missing")

        monkeypatch.setattr("backend.env.load_env", missing)
        assert rb.main([]) == 2
        assert "overlay" in capsys.readouterr().err

    def test_refuses_in_paper_mode(self, monkeypatch, capsys):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setattr("backend.database.TRADING_MODE", "paper")
        assert rb.main([]) == 2
        assert "live database" in capsys.readouterr().err

    def test_refuses_without_an_api_key(self, monkeypatch, capsys):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setattr("backend.database.TRADING_MODE", "live")
        monkeypatch.delenv(rb.API_KEY_VAR, raising=False)
        assert rb.main([]) == 2
        assert rb.API_KEY_VAR in capsys.readouterr().err

    def test_happy_path_records_and_pushes(self, monkeypatch, capsys, maker, tmp_path):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setenv(rb.API_KEY_VAR, "secret-key")
        path, digest = _write_snapshot_folder(tmp_path)
        _, m = maker
        asyncio.run(_seed(maker))
        asyncio.run(_record_snapshot(m, path, digest))
        monkeypatch.setattr(rb, "AnthropicLLM", lambda api_key: FakeLLM("nothing new", []))
        pushed = {}
        monkeypatch.setattr(
            "backend.operator.send_ntfy", lambda title, body, priority="default": pushed.update(title=title, body=body)
        )
        assert rb.main([]) == 0
        assert pushed["title"].startswith("basis research brief")
        assert "nothing new" in capsys.readouterr().out

    def test_a_skip_still_pushes(self, monkeypatch, maker):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setenv(rb.API_KEY_VAR, "secret-key")
        asyncio.run(_seed(maker))
        pushed = {}
        monkeypatch.setattr(
            "backend.operator.send_ntfy", lambda title, body, priority="default": pushed.update(title=title, body=body)
        )
        assert rb.main([]) == 0
        assert "skipped" in pushed["title"]

    def test_crash_is_alerted_and_exits_nonzero(self, monkeypatch, maker):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setenv(rb.API_KEY_VAR, "secret-key")
        asyncio.run(_seed(maker))

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(rb, "run_research_brief", boom)
        alerted = {}
        monkeypatch.setattr(
            "backend.operator.alert_crash",
            lambda title, body, priority="urgent", event_type="CRASH_ALERT": alerted.update(title=title, body=body),
        )
        assert rb.main([]) == 4
        assert alerted["title"] == "basis research brief CRASHED"


async def _async_true() -> bool:
    return True


async def _async_false() -> bool:
    return False
