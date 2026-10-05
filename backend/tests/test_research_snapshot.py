"""#1131: the research snapshot script. No network: a fake SnapshotSources
for the build, httpx.MockTransport for HttpSources. The fail-loud paths are
the point: any input that could not be fetched makes the snapshot
INCOMPLETE, with a reason, and the folder still hashes and verifies."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import research_snapshot as rs
from backend.models import ResearchSnapshotModel

NOW = datetime(2026, 10, 5, 21, 45, tzinfo=UTC)  # 17:45 ET, Monday


def _row(symbol, *, cap="1500000000", price="$20.00", volume="500000", country="United States", name="Acme Corp"):
    return {
        "symbol": symbol,
        "name": name,
        "lastsale": price,
        "volume": volume,
        "marketCap": cap,
        "country": country,
        "sector": "Industrials",
        "industry": "Machinery",
    }


def _submissions(*filings):
    keys = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument")
    return {"filings": {"recent": {k: [f[i] for f in filings] for i, k in enumerate(keys)}}}


class FakeSources:
    def __init__(self, rows=None, ciks=None, subs=None, fail_docs=(), fail_prices=(), screener_error=None):
        self.rows = (
            rows
            if rows is not None
            else [_row("ABCD"), _row("WXYZ")] + [_row(f"F{i:04d}", cap="1") for i in range(3000)]
        )
        self.ciks = ciks if ciks is not None else {"ABCD": "0000000001", "WXYZ": "0000000002", "HELD": "0000000003"}
        self.subs = subs or {
            "0000000001": _submissions(
                ("0000000001-26-000010", "2026-10-02", "2026-10-02T16:05:00.000Z", "8-K", "abcd8k.htm"),
                ("0000000001-26-000009", "2026-09-10", "2026-09-10T16:05:00.000Z", "10-Q", "abcd10q.htm"),
            ),
            "0000000002": _submissions(),
            "0000000003": _submissions(
                ("0000000003-26-000001", "2026-10-05", "2026-10-05T08:00:00.000Z", "10-Q", "held10q.htm"),
            ),
        }
        self.fail_docs = set(fail_docs)
        self.fail_prices = set(fail_prices)
        self.screener_error = screener_error

    def screener_rows(self):
        if self.screener_error:
            raise rs.SourceError(self.screener_error)
        return self.rows

    def ticker_ciks(self):
        return self.ciks

    def submissions(self, cik):
        if cik not in self.subs:
            raise rs.SourceError(f"CIK {cik}: HTTP 404")
        return self.subs[cik]

    def filing_index(self, cik, accession):
        return '<table><tr><td>1</td><td>Press release</td><td><a href="ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>9</td></tr></table>'

    def document(self, url):
        if any(f in url for f in self.fail_docs):
            raise rs.SourceError(f"{url}: HTTP 503 after 5 attempts")
        return "<html><body><p>Item 2. Management&#8217;s Discussion</p><p>Revenue rose.</p></body></html>"

    def price_history(self, symbol):
        if symbol in self.fail_prices:
            raise rs.SourceError(f"{symbol}: no price history")
        return [("2026-10-02", 19.0, 1000.0), ("2026-10-05", 20.5, 1200.0)]


def _build(tmp_path, sources=None, held=(), kind="NIGHTLY"):
    return rs.build_snapshot_folder(
        sources or FakeSources(), kind=kind, root=tmp_path / "snaps", now=NOW, held_symbols=list(held)
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_number(self):
        assert rs.parse_number("$1,234.50") == 1234.5
        assert rs.parse_number("5.6%") == 5.6
        assert rs.parse_number(7) == 7.0
        for junk in ("", "NA", "n/a", "abc", None, True, [1]):
            assert rs.parse_number(junk) is None

    def test_symbols(self):
        assert rs.normalize_symbol(" brk/b ") == "BRK-B"
        assert rs.normalize_symbol("BF.B") == "BF-B"
        assert rs.is_non_common("ABR^D", "Arbor")
        assert rs.is_non_common("XYZW", "Blank Check Acquisition Corp")
        assert rs.is_non_common("ABCDW", "Acme")  # 5+ char warrant suffix
        assert rs.is_non_common("ABCPRC", "Acme")
        assert not rs.is_non_common("PLOW", "Douglas Dynamics")  # a 4-char root is not a suffix
        assert not rs.is_non_common("ABCD", "Acme Corp")

    def test_screen_universe(self):
        rows = [
            _row("ABCD"),
            _row("ABCD"),  # duplicate listing
            _row("BIGG", cap="50000000000"),
            _row("TINY", cap="100000000"),
            _row("PENY", price="$2.00"),
            _row("THIN", volume="1000"),
            _row("FRGN", country="Canada"),
            _row("BADD", cap=""),
            _row("NOCK"),
            _row("WRNTW"),
            {"symbol": "", "country": "United States"},
        ]
        universe, no_cik = rs.screen_universe(rows, {"ABCD": "1"})
        assert [m.symbol for m in universe] == ["ABCD"]
        assert universe[0].dollar_volume == 20.0 * 500000
        assert no_cik == ["NOCK"]

    def test_select_filings(self):
        subs = _submissions(
            ("a1", "2026-10-02", "2026-10-02T09:00:00.000Z", "8-K", "a.htm"),
            ("a2", "2026-10-03", "2026-10-03T09:00:00.000Z", "8-K", "b.htm"),  # latest 8-K wins
            ("a3", "2026-10-03", "2026-10-03T09:00:00.000Z", "8-K/A", "c.htm"),  # amendments are not the form
            ("a4", "2026-09-01", "2026-09-01T09:00:00.000Z", "10-Q", "d.htm"),  # before the window
            ("a5", "2026-10-05", "2026-10-05T22:00:00.000Z", "10-K", "e.htm"),  # 18:00 ET: after the 17:45 ET cutoff
            ("a8", "2026-10-05", "2026-10-05T21:00:00", "10-K", "h.htm"),  # naive reads as UTC: 17:00 ET, kept
            ("a6", "bad-date", "", "10-Q", "f.htm"),
            ("a7", "2026-10-04", "garbage", "10-Q", "g.htm"),
        )
        refs = rs.select_filings("1", subs, date(2026, 10, 1), NOW.isoformat())
        assert [(r.form, r.accession) for r in refs] == [("8-K", "a2"), ("10-Q", "a7"), ("10-K", "a8")]
        with pytest.raises(rs.SourceError, match="no recent filings"):
            rs.select_filings("1", {"filings": {}}, date(2026, 10, 1), NOW.isoformat())

    def test_text_and_excerpt(self):
        assert rs.html_to_text("<script>x()</script><p>A&amp;B</p>  <b>c</b>") == "A&B c"
        toc = "Item 2. Management's Discussion ... page 5. "
        body = "Item 2. Management's Discussion and Analysis. Sales grew."
        mda = rs.excerpt("cover " + toc + "notes " + body, "10-Q")
        assert mda.startswith("Item 2.") and "page 5" not in mda and "Sales grew." in mda
        assert rs.excerpt("cover only " + body, "10-Q").startswith("Item 2.")
        assert rs.excerpt("plain 8-K text", "8-K") == "plain 8-K text"
        assert len(rs.excerpt("x" * (rs.EXCERPT_CHARS + 50), "10-K")) == rs.EXCERPT_CHARS

    def test_find_ex99_href(self):
        index = (
            '<tr><td>1</td><td><a href="/ix?doc=/Archives/edgar/data/1/a/main.htm">main</a></td><td>8-K</td></tr>'
            '<tr><td>2</td><td><a href="ex992.htm">ex992</a></td><td>EX-99.2</td></tr>'
            '<tr><td>3</td><td><a href="/ix?doc=/Archives/x/ex991.htm">ex991</a></td><td>EX-99.1</td></tr>'
        )
        assert rs.find_ex99_href(index) == "/Archives/x/ex991.htm"
        assert rs.find_ex99_href("<tr><td>no exhibits</td></tr>") is None

    def test_archive_url(self):
        assert (
            rs.archive_url("0000000001", "0001-26-1", "doc.htm")
            == "https://www.sec.gov/Archives/edgar/data/1/0001261/doc.htm"
        )
        assert rs.archive_url("1", "a", "/Archives/x.htm") == "https://www.sec.gov/Archives/x.htm"
        assert rs.archive_url("1", "a", "https://example.invalid/x") == "https://example.invalid/x"

    def test_snapshot_root(self, monkeypatch, tmp_path):
        monkeypatch.delenv(rs.ROOT_VAR, raising=False)
        assert rs.snapshot_root(None) == rs.DEFAULT_ROOT
        monkeypatch.setenv(rs.ROOT_VAR, str(tmp_path))
        assert rs.snapshot_root(None) == tmp_path
        assert rs.snapshot_root(str(tmp_path / "x")) == tmp_path / "x"


# ---------------------------------------------------------------------------
# Building a snapshot
# ---------------------------------------------------------------------------


class TestBuild:
    def test_complete_snapshot_verifies(self, tmp_path):
        result = _build(tmp_path, held=["held"])
        assert result.status == "COMPLETE" and result.reasons == []
        assert (result.as_of, result.snapshot_id) == ("2026-10-05", "20261005T214500Z")
        folder = Path(result.path)
        assert folder.name == result.snapshot_id and not (tmp_path / "snaps" / f"{result.snapshot_id}.partial").exists()
        manifest = json.loads((folder / rs.MANIFEST).read_text(encoding="utf-8"))
        assert manifest["content_hash"] == result.content_hash and manifest["status"] == "COMPLETE"
        filings = json.loads((folder / "filings.json").read_text(encoding="utf-8"))["filings"]
        assert {(f["symbol"], f["form"]) for f in filings} == {("ABCD", "8-K"), ("HELD", "10-Q")}
        abcd_8k = next(f for f in filings if f["form"] == "8-K")
        assert abcd_8k["url"].endswith("/ex991.htm")  # the press release, not the cover document
        prices = rs.load_snapshot_prices(result.path, result.content_hash)
        assert prices["ABCD"] == 20.0  # the screen's frozen last sale stays the snapshot price
        assert prices["HELD"] == 20.5 and prices["SPY"] == 20.5  # off-screen names take the last history close
        assert "WXYZ" in prices and result.counts["held"] == 1 and result.counts["universe"] == 2

    def test_tampering_breaks_verification(self, tmp_path):
        result = _build(tmp_path)
        folder = Path(result.path)
        (folder / "prices.json").write_text("{}", encoding="utf-8")
        with pytest.raises(rs.SnapshotIntegrityError, match="differ from the manifest"):
            rs.verify_snapshot(result.path, result.content_hash)
        with pytest.raises(rs.SnapshotIntegrityError, match="missing"):
            rs.verify_snapshot(str(tmp_path / "nowhere"), "x")

    def test_wrong_hash_and_bad_manifest(self, tmp_path):
        result = _build(tmp_path)
        with pytest.raises(rs.SnapshotIntegrityError, match="content hash"):
            rs.verify_snapshot(result.path, "0" * 64)
        (Path(result.path) / rs.MANIFEST).write_text("not json", encoding="utf-8")
        with pytest.raises(rs.SnapshotIntegrityError, match="manifest unreadable"):
            rs.load_snapshot_prices(result.path, result.content_hash)

    def test_document_failure_is_incomplete(self, tmp_path):
        result = _build(tmp_path, FakeSources(fail_docs={"ex991"}))
        assert result.status == "INCOMPLETE"
        assert any(r.startswith("filings (documents): 1 of 1 failed") for r in result.reasons)
        manifest = json.loads((Path(result.path) / rs.MANIFEST).read_text(encoding="utf-8"))
        assert manifest["status"] == "INCOMPLETE" and manifest["reasons"] == result.reasons
        rs.verify_snapshot(result.path, result.content_hash)  # partial, but still exactly what it says

    def test_price_and_submission_failures_are_incomplete(self, tmp_path):
        sources = FakeSources(fail_prices={"SPY"})
        del sources.subs["0000000002"]
        result = _build(tmp_path, sources)
        assert any(r.startswith("prices: 1 of") for r in result.reasons)
        assert any(r.startswith("filings (submissions): 1 of 2 failed") for r in result.reasons)

    def test_truncated_or_failed_screen_is_incomplete(self, tmp_path):
        short = _build(tmp_path / "a", FakeSources(rows=[_row("ABCD")]))
        assert any("under 3000" in r for r in short.reasons)
        empty = _build(tmp_path / "b", FakeSources(rows=[_row(f"F{i:04d}", cap="1") for i in range(3000)]))
        assert "universe: the screen kept no names" in empty.reasons
        broken = _build(tmp_path / "c", FakeSources(screener_error="Nasdaq screener: no rows"))
        assert broken.reasons[0].startswith("universe: SourceError")

    def test_held_pick_without_a_cik_is_incomplete(self, tmp_path):
        result = _build(tmp_path, held=["GONE", "ABCD"])
        assert any("held picks: 1 of 2 failed — GONE has no SEC CIK" in r for r in result.reasons)

    def test_many_failures_are_summarized(self):
        line = rs._summarize("prices", [f"S{i}" for i in range(5)], 9)
        assert line == "prices: 5 of 9 failed — S0; S1; S2 (+2 more)"

    def test_monthly_reaches_further_back(self, tmp_path):
        result = _build(tmp_path, kind="MONTHLY")
        filings = json.loads((Path(result.path) / "filings.json").read_text(encoding="utf-8"))
        assert filings["since"] == "2026-08-31"
        assert {(f["symbol"], f["form"]) for f in filings["filings"]} == {("ABCD", "8-K"), ("ABCD", "10-Q")}

    def test_leftover_partial_folder_is_replaced(self, tmp_path):
        stale = tmp_path / "snaps" / "20261005T214500Z.partial"
        stale.mkdir(parents=True)
        (stale / "junk.txt").write_text("x", encoding="utf-8")
        result = _build(tmp_path)
        assert not (Path(result.path) / "junk.txt").exists()


# ---------------------------------------------------------------------------
# take_snapshot: the database row
# ---------------------------------------------------------------------------


@pytest.fixture
def maker(tmp_path, monkeypatch):
    import backend.database as db_mod

    url = f"sqlite+aiosqlite:///{(tmp_path / 'snap.db').as_posix()}"
    monkeypatch.setattr(db_mod, "DATABASE_URL", url)
    engine = create_async_engine(url)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(db_mod, "async_session_maker", m)
    monkeypatch.setattr(db_mod, "TRADING_MODE", "live")
    return db_mod, m


class TestTakeSnapshot:
    @pytest.mark.asyncio
    async def test_records_the_row(self, maker, tmp_path):
        db_mod, m = maker
        await db_mod.init_db()
        result = await rs.take_snapshot(m, FakeSources(), kind="NIGHTLY", root=tmp_path / "snaps", now=NOW)
        async with m() as session:
            row = (await session.execute(select(ResearchSnapshotModel))).scalar_one()
        assert (row.id, row.status, row.content_hash, row.path) == (
            result.snapshot_id,
            "COMPLETE",
            result.content_hash,
            result.path,
        )

    @pytest.mark.asyncio
    async def test_a_folder_that_cannot_be_written_is_still_recorded(self, maker, tmp_path):
        db_mod, m = maker
        await db_mod.init_db()
        blocker = tmp_path / "file-not-dir"
        blocker.write_text("x", encoding="utf-8")
        result = await rs.take_snapshot(m, FakeSources(), kind="NIGHTLY", root=blocker, now=NOW)
        async with m() as session:
            row = (await session.execute(select(ResearchSnapshotModel))).scalar_one()
        assert result.status == row.status == "INCOMPLETE"
        assert row.path == "" and row.reasons[0].startswith("snapshot folder not written")


# ---------------------------------------------------------------------------
# HttpSources, against a mock transport
# ---------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestHttpSources:
    def test_retries_then_succeeds_with_the_sec_user_agent(self):
        calls: list[httpx.Request] = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"0": {"ticker": "brk.b", "cik_str": 1067983}, "1": {"bad": 1}})

        src = rs.HttpSources("ops@example.invalid", client=_client(handler), sleep=lambda s: None)
        assert src.ticker_ciks() == {"BRK-B": "0001067983"}
        assert calls[0].headers["User-Agent"] == "basis-research ops@example.invalid"

    def test_gives_up_after_retries_and_on_hard_errors(self):
        src = rs.HttpSources("c", client=_client(lambda r: httpx.Response(503)), sleep=lambda s: None)
        with pytest.raises(rs.SourceError, match="HTTP 503 after 5 attempts"):
            src.submissions("0000000001")
        src = rs.HttpSources("c", client=_client(lambda r: httpx.Response(404)), sleep=lambda s: None)
        with pytest.raises(rs.SourceError, match="HTTP 404"):
            src.filing_index("1", "0001-26-1")

        def boom(request):
            raise httpx.ConnectError("down")

        src = rs.HttpSources("c", client=_client(boom), sleep=lambda s: None)
        with pytest.raises(rs.SourceError, match="ConnectError"):
            src.document("https://www.sec.gov/x.htm")

    def test_document_is_capped(self, monkeypatch):
        monkeypatch.setattr(rs, "MAX_DOCUMENT_BYTES", 10)
        src = rs.HttpSources(
            "c", client=_client(lambda r: httpx.Response(200, content=b"x" * 100)), sleep=lambda s: None
        )
        assert len(src.document("https://www.sec.gov/x.htm")) >= 10  # stops reading at the first chunk past the cap

    def test_screener_and_submissions_shapes(self):
        def handler(request):
            if "nasdaq" in request.url.host:
                return httpx.Response(200, json={"data": {"rows": [_row("ABCD"), "junk"]}})
            return httpx.Response(200, json=[1, 2])

        src = rs.HttpSources("c", client=_client(handler), sleep=lambda s: None)
        assert [r["symbol"] for r in src.screener_rows()] == ["ABCD"]
        with pytest.raises(rs.SourceError, match="not an object"):
            src.submissions("1")
        with pytest.raises(rs.SourceError, match="unexpected shape"):
            src.ticker_ciks()
        bad = rs.HttpSources(
            "c", client=_client(lambda r: httpx.Response(200, json={"data": None})), sleep=lambda s: None
        )
        with pytest.raises(rs.SourceError, match="no rows"):
            bad.screener_rows()
        text = rs.HttpSources("c", client=_client(lambda r: httpx.Response(200, text="<html>")), sleep=lambda s: None)
        with pytest.raises(rs.SourceError, match="not JSON"):
            text.submissions("1")

    def test_submissions_ok(self):
        src = rs.HttpSources(
            "c", client=_client(lambda r: httpx.Response(200, json={"filings": {}})), sleep=lambda s: None
        )
        assert src.submissions("1") == {"filings": {}}

    def test_price_history(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1759411800, 1759498200, 1759757400],
                        "indicators": {"quote": [{"close": [10.0, None, 11.5], "volume": [100, 200, None]}]},
                    }
                ]
            }
        }
        src = rs.HttpSources("c", client=_client(lambda r: httpx.Response(200, json=payload)), sleep=lambda s: None)
        bars = src.price_history("ABCD")
        assert [b[1:] for b in bars] == [(10.0, 100.0), (11.5, 0.0)]
        empty = rs.HttpSources(
            "c", client=_client(lambda r: httpx.Response(200, json={"chart": {"result": []}})), sleep=lambda s: None
        )
        with pytest.raises(rs.SourceError, match="no price history"):
            empty.price_history("ABCD")
        no_close = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"close": [None]}]}}]}}
        nc = rs.HttpSources("c", client=_client(lambda r: httpx.Response(200, json=no_close)), sleep=lambda s: None)
        with pytest.raises(rs.SourceError, match="no closes"):
            nc.price_history("ABCD")

    def test_pacing_sleeps_between_sec_requests(self):
        slept: list[float] = []
        src = rs.HttpSources("c", client=_client(lambda r: httpx.Response(200, json={})), sleep=slept.append)
        src.submissions("1")
        src.submissions("2")
        assert slept and all(0 < s <= 1.0 / rs.SEC_MAX_REQUESTS_PER_SECOND for s in slept)


# ---------------------------------------------------------------------------
# The CLI refusals
# ---------------------------------------------------------------------------


class TestMain:
    def test_refuses_in_paper_mode(self, monkeypatch, capsys):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setattr("backend.database.TRADING_MODE", "paper")
        assert rs.main([]) == 2
        assert "live database" in capsys.readouterr().err

    def test_refuses_without_a_sec_contact(self, monkeypatch, capsys):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setattr("backend.database.TRADING_MODE", "live")
        monkeypatch.delenv(rs.SEC_CONTACT_VAR, raising=False)
        assert rs.main([]) == 2
        assert rs.SEC_CONTACT_VAR in capsys.readouterr().err

    def test_refuses_on_a_missing_overlay(self, monkeypatch, capsys):
        def missing():
            raise RuntimeError("overlay .env.live is missing")

        monkeypatch.setattr("backend.env.load_env", missing)
        assert rs.main([]) == 2
        assert "overlay" in capsys.readouterr().err

    def test_runs_and_reports(self, monkeypatch, capsys, maker, tmp_path):
        monkeypatch.setattr("backend.env.load_env", lambda: None)
        monkeypatch.setenv(rs.SEC_CONTACT_VAR, "ops@example.invalid")
        monkeypatch.setattr(rs, "HttpSources", lambda contact: FakeSources(fail_prices={"SPY"}))
        assert rs.main(["--kind", "monthly", "--root", str(tmp_path / "out")]) == 1
        out = capsys.readouterr().out
        assert "(MONTHLY, as of" in out and "INCOMPLETE: prices" in out
