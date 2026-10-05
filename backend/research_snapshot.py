"""research_snapshot.py — freeze the research brief's inputs (#1131).

    pixi run research-snapshot                  # nightly (default): filings from the last few days
    pixi run research-snapshot --kind monthly   # the monthly screen: a month of filings

No AI runs here. This is the "snapshot" half of snapshot-then-analyze
(spec/research-brief.md): a plain script that writes everything the brief
may read into one timestamped folder OUTSIDE the repo, then records the
folder's hash and as_of date in the live database. The AI later reads ONLY
that folder, with web access off, so a brief written late can never see
anything after the snapshot's date. A missed slot simply runs at the next
opportunity and is dated when it actually ran.

Inputs, and where they come from (no credentials, public sources only):

- The universe screen: Nasdaq's public stock screener download
  (api.nasdaq.com/api/screener/stocks, the data behind
  nasdaq.com/market-activity/stocks/screener), one request covering every
  NASDAQ, NYSE and NYSE American listing with last sale, volume, market
  cap, country and sector. Kept: country United States; market cap in the
  small- and mid-cap band (SCREEN_MIN_MARKET_CAP..SCREEN_MAX_MARKET_CAP);
  last sale at least SCREEN_MIN_PRICE; one-day dollar volume (last sale x
  volume) at least SCREEN_MIN_DOLLAR_VOLUME, the liquidity filter; common
  stock only (warrants, units, rights, preferreds and SPAC shells dropped by
  the #1137 name and suffix rules); and an SEC registrant (the ticker maps to
  a CIK in SEC's company_tickers.json) — a name with no CIK has no filings to
  read, so it is outside the universe by definition and listed as excluded.
- Recent prices: every universe member's last sale from the screen; for the
  focus set (held picks, every company with a filing in the window, and the
  SPY benchmark) six months of daily closes from Yahoo's public chart
  endpoint, the same source backend/dividend_history.py uses.
- Recent filings: SEC EDGAR. Per company, the submissions JSON; within the
  lookback window, the latest 8-K, 10-Q and 10-K accepted before the run
  started; a text excerpt of each (an 8-K's EX-99 press release when it has
  one, a 10-Q/10-K's MD&A section). The EX-99 lookup reads the filing index's
  Type column, the pattern from #1082's text-signals fetcher.
- Held picks: the operator picks book's current holdings, so each held
  pick's filings and prices are in every snapshot even after it leaves the
  screen.

Fail loud, never partial-as-complete: any input that could not be fetched
makes the snapshot INCOMPLETE, with the reasons in its manifest and its
database row. Whatever was fetched is still written (it is evidence of what
the run saw), but no brief may be recorded against it (research.record_brief).

SEC fair access: at most SEC_MAX_REQUESTS_PER_SECOND, with a User-Agent that
names a contact address (BASIS_SEC_CONTACT in .env — never in the repo).

Intended schedule (phase 2 registers it; nothing is scheduled here): nightly
after 17:30 ET, clear of the 18:30-19:30 evening executor window; the
monthly kind on the first trading evening of each month.
"""

import argparse
import asyncio
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.dates import MARKET_TZ
from backend.states import (
    RESEARCH_KIND_MONTHLY,
    RESEARCH_KIND_NIGHTLY,
    SNAPSHOT_COMPLETE_STATUS,
    SNAPSHOT_INCOMPLETE_STATUS,
)

# --- The universe screen (documented in the module docstring) ---------------
SCREEN_MIN_MARKET_CAP = 300e6
SCREEN_MAX_MARKET_CAP = 10e9
SCREEN_MIN_PRICE = 5.0
SCREEN_MIN_DOLLAR_VOLUME = 5e6
SCREEN_COUNTRY = "United States"
# A screener download this small is a truncated or broken response, not the
# US market: the full listing is several thousand rows.
MIN_SCREENER_ROWS = 3000

# --- Filings -------------------------------------------------------------
FILING_FORMS = ("8-K", "10-Q", "10-K")
# Nightly reaches back over a weekend plus a holiday; monthly covers a month.
LOOKBACK_DAYS = {RESEARCH_KIND_NIGHTLY: 4, RESEARCH_KIND_MONTHLY: 35}
EXCERPT_CHARS = 20_000
MAX_DOCUMENT_BYTES = 8_000_000

# --- Prices ---------------------------------------------------------------
BENCHMARK_SYMBOLS = ("SPY",)
PRICE_HISTORY_RANGE = "6mo"

# --- Where, and who ---------------------------------------------------------
DEFAULT_ROOT = Path.home() / "basis-data" / "research" / "snapshots"
ROOT_VAR = "BASIS_RESEARCH_SNAPSHOT_DIR"
SEC_CONTACT_VAR = "BASIS_SEC_CONTACT"
SEC_MAX_REQUESTS_PER_SECOND = 8  # SEC's published ceiling is 10
_PUBLIC_MIN_INTERVAL_S = 0.4
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_ATTEMPTS = 5
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) basis-research/1.0"

MANIFEST = "manifest.json"
_REASON_EXAMPLES = 3

_BAD_NAME_RE = re.compile(
    r"acquisition corp|acquisition corporation|\bwarrants?\b|\bunits?\b|\bpreferred\b|\brights?\b|depositary",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"(WS|W|UN|U|RT|R)$")
_PREFERRED_RE = re.compile(r"PR[A-Z0-9]$")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_MDA_RE = {
    "10-Q": re.compile(r"item\s*2\.?\s*[-:]?\s*management.{0,3}s\s+discussion", re.IGNORECASE),
    "10-K": re.compile(r"item\s*7\.?\s*[-:]?\s*management.{0,3}s\s+discussion", re.IGNORECASE),
}


class SourceError(RuntimeError):
    """A public source could not be read after retries."""


class SnapshotIntegrityError(RuntimeError):
    """A snapshot folder no longer matches its recorded hash, or is unreadable."""


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    name: str
    cik: str
    market_cap: float
    last_sale: float
    volume: float
    dollar_volume: float
    sector: str
    industry: str


@dataclass(frozen=True)
class FilingRef:
    cik: str
    form: str
    accession: str
    filing_date: str
    accepted_at: str
    primary_document: str


@dataclass
class SnapshotResult:
    snapshot_id: str
    kind: str
    as_of: str
    created_at: str
    path: str
    content_hash: str
    status: str
    reasons: list[str]
    counts: dict[str, int] = field(default_factory=dict)


class SnapshotSources(Protocol):
    """Every network read the snapshot makes. Tests pass a fake."""

    def screener_rows(self) -> list[dict[str, object]]: ...
    def ticker_ciks(self) -> dict[str, str]: ...
    def submissions(self, cik: str) -> dict[str, object]: ...
    def filing_index(self, cik: str, accession: str) -> str: ...
    def document(self, url: str) -> str: ...
    def price_history(self, symbol: str) -> list[tuple[str, float, float]]: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_number(raw: object) -> float | None:
    """'$12.34', '1,234', '5.6%' or a number -> float; blanks and junk -> None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if not isinstance(raw, str):
        return None
    cleaned = raw.replace("$", "").replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned.upper() in {"NA", "N/A"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_symbol(symbol: str) -> str:
    """Nasdaq writes share classes as BRK/B; SEC and Yahoo write BRK-B."""
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def is_non_common(symbol: str, name: str) -> bool:
    """Warrants, units, rights, preferreds, depositary shares and SPAC shells
    are not "the stock" (#1137's declared rules): by name, by a '^' preferred
    marker, or by the Nasdaq/NYSE suffix convention on 5+ character tickers."""
    s = symbol.strip().upper()
    if "^" in s or _BAD_NAME_RE.search(name or ""):
        return True
    root = s.replace("-", "").replace("/", "")
    return len(root) >= 5 and bool(_PREFERRED_RE.search(root) or _SUFFIX_RE.search(root))


def screen_universe(rows: list[dict[str, object]], ciks: dict[str, str]) -> tuple[list[UniverseMember], list[str]]:
    """Apply the documented screen. Returns (universe, excluded_no_cik)."""
    universe: list[UniverseMember] = []
    no_cik: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw_symbol = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or "")
        if not raw_symbol or str(row.get("country") or "").strip() != SCREEN_COUNTRY:
            continue
        if is_non_common(raw_symbol, name):
            continue
        cap = parse_number(row.get("marketCap"))
        price = parse_number(row.get("lastsale"))
        volume = parse_number(row.get("volume"))
        if cap is None or price is None or volume is None:
            continue
        if not (SCREEN_MIN_MARKET_CAP <= cap <= SCREEN_MAX_MARKET_CAP) or price < SCREEN_MIN_PRICE:
            continue
        if price * volume < SCREEN_MIN_DOLLAR_VOLUME:
            continue
        symbol = normalize_symbol(raw_symbol)
        if symbol in seen:
            continue
        seen.add(symbol)
        cik = ciks.get(symbol)
        if cik is None:
            no_cik.append(symbol)
            continue
        universe.append(
            UniverseMember(
                symbol=symbol,
                name=name,
                cik=cik,
                market_cap=cap,
                last_sale=price,
                volume=volume,
                dollar_volume=price * volume,
                sector=str(row.get("sector") or ""),
                industry=str(row.get("industry") or ""),
            )
        )
    return sorted(universe, key=lambda m: m.symbol), sorted(no_cik)


def select_filings(cik: str, submissions: dict[str, object], since: date, cutoff_iso: str) -> list[FilingRef]:
    """Per form in FILING_FORMS, the latest filing dated on or after *since*
    and accepted no later than *cutoff_iso* (the run's start, so the folder is
    frozen as of that moment). Amendments (/A) are not the form itself."""
    filings = submissions.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise SourceError(f"CIK {cik}: submissions JSON has no recent filings block")
    forms = recent.get("form") or []
    n = len(forms)

    def col(name: str) -> list[object]:
        values = recent.get(name) or []
        return list(values) + [""] * (n - len(values))

    accession, filed, accepted, primary = (
        col("accessionNumber"),
        col("filingDate"),
        col("acceptanceDateTime"),
        col("primaryDocument"),
    )
    latest: dict[str, FilingRef] = {}
    cutoff = datetime.fromisoformat(cutoff_iso)
    for i, form in enumerate(forms):
        if form not in FILING_FORMS:
            continue
        filing_date = str(filed[i])
        try:
            if date.fromisoformat(filing_date) < since:
                continue
        except ValueError:
            continue
        accepted_at = str(accepted[i] or "")
        if accepted_at:
            # EDGAR's acceptanceDateTime is UTC ("...Z"): an after-close 8-K
            # reads 20:16Z, i.e. 16:16 ET. Unparseable reads as no time.
            try:
                accepted_dt = datetime.fromisoformat(accepted_at)
                if accepted_dt.tzinfo is None:
                    accepted_dt = accepted_dt.replace(tzinfo=UTC)
            except ValueError:
                accepted_dt = None
            if accepted_dt is not None and accepted_dt > cutoff:
                continue
        ref = FilingRef(
            cik=cik,
            form=str(form),
            accession=str(accession[i]),
            filing_date=filing_date,
            accepted_at=accepted_at,
            primary_document=str(primary[i]),
        )
        current = latest.get(ref.form)
        if current is None or (ref.filing_date, ref.accepted_at) > (current.filing_date, current.accepted_at):
            latest[ref.form] = ref
    return [latest[f] for f in FILING_FORMS if f in latest]


def html_to_text(raw: str) -> str:
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html_lib.unescape(text)).strip()


def excerpt(text: str, form: str) -> str:
    """A 10-Q/10-K's MD&A (the heading's LAST occurrence before the text runs
    out, since the first is usually the table of contents), else the start."""
    pattern = _MDA_RE.get(form)
    start = 0
    if pattern is not None:
        matches = [m.start() for m in pattern.finditer(text)]
        if len(matches) >= 2:
            start = matches[1]
        elif matches:
            start = matches[0]
    return text[start : start + EXCERPT_CHARS]


def find_ex99_href(index_html: str) -> str | None:
    """The EX-99 exhibit's href from an EDGAR filing index, by its Type cell
    (EDGAR's own classification), preferring EX-99.1."""
    candidates: list[tuple[str, str]] = []
    for row in _ROW_RE.findall(index_html):
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row)]
        ex_type = next((c for c in cells if re.match(r"(?i)^EX-?99", c)), None)
        href = _HREF_RE.search(row)
        if ex_type and href:
            link = href.group(1)
            if link.lower().startswith("/ix?doc="):
                link = link[len("/ix?doc=") :]
            candidates.append((ex_type.upper(), link))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (0 if t[0].rstrip(".") in ("EX-99.1", "EX-991", "EX-99") else 1, t[0]))
    return candidates[0][1]


def archive_url(cik: str, accession: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www.sec.gov{href}"
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{href}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash_of(files: dict[str, str]) -> str:
    """The snapshot's hash: sha256 over the canonical JSON of its per-file
    hashes (relative path -> sha256), so any byte changed anywhere changes it."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _content_files(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): sha256_file(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    }


def verify_snapshot(path: str, content_hash: str) -> Path:
    """Raise SnapshotIntegrityError unless the folder's files hash to exactly
    *content_hash* and match its manifest. Returns the folder."""
    folder = Path(path)
    if not folder.is_dir():
        raise SnapshotIntegrityError(f"folder {folder} is missing")
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotIntegrityError(f"manifest unreadable: {exc}") from exc
    files = _content_files(folder)
    if files != manifest.get("files"):
        raise SnapshotIntegrityError("files on disk differ from the manifest")
    if content_hash_of(files) != content_hash:
        raise SnapshotIntegrityError("content hash differs from the recorded hash")
    return folder


def load_snapshot_prices(path: str, content_hash: str) -> dict[str, float]:
    """symbol -> frozen close, after the integrity check."""
    folder = verify_snapshot(path, content_hash)
    try:
        prices = json.loads((folder / "prices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotIntegrityError(f"prices.json unreadable: {exc}") from exc
    return {s: float(p["close"]) for s, p in prices.items() if isinstance(p, dict) and p.get("close") is not None}


def _summarize(stage: str, failures: list[str], attempted: int) -> str:
    shown = "; ".join(failures[:_REASON_EXAMPLES])
    more = f" (+{len(failures) - _REASON_EXAMPLES} more)" if len(failures) > _REASON_EXAMPLES else ""
    return f"{stage}: {len(failures)} of {attempted} failed — {shown}{more}"


# ---------------------------------------------------------------------------
# The real sources
# ---------------------------------------------------------------------------


class HttpSources:
    """Public HTTP sources with retries. SEC requests are paced to
    SEC_MAX_REQUESTS_PER_SECOND and carry the contact User-Agent SEC asks for."""

    def __init__(self, contact: str, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self._sec_ua = f"basis-research {contact}"
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._sleep = sleep
        self._last: dict[str, float] = {"sec": 0.0, "public": 0.0}

    def _pace(self, lane: str) -> None:
        interval = 1.0 / SEC_MAX_REQUESTS_PER_SECOND if lane == "sec" else _PUBLIC_MIN_INTERVAL_S
        wait = self._last[lane] + interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last[lane] = time.monotonic()

    def _get(self, url: str, *, lane: str, params: dict[str, str] | None = None, max_bytes: int | None = None) -> bytes:
        headers = (
            {"User-Agent": self._sec_ua, "Accept-Encoding": "gzip, deflate"}
            if lane == "sec"
            else {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/plain, */*"}
        )
        last_error = "no attempt made"
        for attempt in range(_ATTEMPTS):
            self._pace(lane)
            try:
                with self._client.stream("GET", url, params=params, headers=headers) as resp:
                    if resp.status_code in _RETRY_STATUSES:
                        last_error = f"HTTP {resp.status_code}"
                        self._sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status_code != 200:
                        raise SourceError(f"{url}: HTTP {resp.status_code}")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if max_bytes is not None and len(body) >= max_bytes:
                            break
                    return bytes(body)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep(1.5 * (attempt + 1))
        raise SourceError(f"{url}: {last_error} after {_ATTEMPTS} attempts")

    def _json(self, url: str, *, lane: str, params: dict[str, str] | None = None) -> object:
        try:
            return json.loads(self._get(url, lane=lane, params=params))
        except ValueError as exc:
            raise SourceError(f"{url}: not JSON") from exc

    def screener_rows(self) -> list[dict[str, object]]:
        data = self._json(
            "https://api.nasdaq.com/api/screener/stocks",
            lane="public",
            params={"tableonly": "true", "download": "true"},
        )
        rows = ((data or {}).get("data") or {}).get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise SourceError("Nasdaq screener: no rows in the response")
        return [r for r in rows if isinstance(r, dict)]

    def ticker_ciks(self) -> dict[str, str]:
        data = self._json("https://www.sec.gov/files/company_tickers.json", lane="sec")
        if not isinstance(data, dict):
            raise SourceError("SEC company_tickers.json: unexpected shape")
        out: dict[str, str] = {}
        for v in data.values():
            if isinstance(v, dict) and v.get("ticker") and v.get("cik_str") is not None:
                out.setdefault(normalize_symbol(str(v["ticker"])), str(v["cik_str"]).zfill(10))
        return out

    def submissions(self, cik: str) -> dict[str, object]:
        data = self._json(f"https://data.sec.gov/submissions/CIK{cik}.json", lane="sec")
        if not isinstance(data, dict):
            raise SourceError(f"CIK {cik}: submissions not an object")
        return data

    def filing_index(self, cik: str, accession: str) -> str:
        url = archive_url(cik, accession, f"{accession}-index.html")
        return self._get(url, lane="sec").decode("utf-8", errors="replace")

    def document(self, url: str) -> str:
        return self._get(url, lane="sec", max_bytes=MAX_DOCUMENT_BYTES).decode("utf-8", errors="replace")

    def price_history(self, symbol: str) -> list[tuple[str, float, float]]:
        data = self._json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            lane="public",
            params={"range": PRICE_HISTORY_RANGE, "interval": "1d"},
        )
        results = ((data or {}).get("chart") or {}).get("result") if isinstance(data, dict) else None
        if not results:
            raise SourceError(f"{symbol}: no price history")
        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes, volumes = quote.get("close") or [], quote.get("volume") or []
        bars: list[tuple[str, float, float]] = []
        for i, ts in enumerate(stamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            day = datetime.fromtimestamp(ts, tz=MARKET_TZ).date().isoformat()
            vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
            bars.append((day, float(close), float(vol)))
        if not bars:
            raise SourceError(f"{symbol}: price history has no closes")
        return bars


# ---------------------------------------------------------------------------
# Building one snapshot
# ---------------------------------------------------------------------------


def _write_json(folder: Path, name: str, payload: object) -> None:
    (folder / name).write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


def build_snapshot_folder(
    sources: SnapshotSources,
    *,
    kind: str,
    root: Path,
    now: datetime,
    held_symbols: list[str],
) -> SnapshotResult:
    """Fetch every input into root/<id>, write the manifest, and return the
    verdict. Never raises for a source failure: each one is a reason, and any
    reason makes the snapshot INCOMPLETE. The folder is built as <id>.partial
    and renamed only once its manifest is written."""
    snapshot_id = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    as_of = now.astimezone(MARKET_TZ).date()
    created_at = now.astimezone(UTC).isoformat()
    since = as_of - timedelta(days=LOOKBACK_DAYS[kind])
    work = root / f"{snapshot_id}.partial"
    final = root / snapshot_id
    reasons: list[str] = []
    counts: dict[str, int] = {}
    root.mkdir(parents=True, exist_ok=True)
    if work.exists():
        shutil.rmtree(work)
    (work / "filings").mkdir(parents=True)

    universe: list[UniverseMember] = []
    ciks: dict[str, str] = {}
    try:
        rows = sources.screener_rows()
        counts["screener_rows"] = len(rows)
        if len(rows) < MIN_SCREENER_ROWS:
            reasons.append(f"universe: the screener returned {len(rows)} rows, under {MIN_SCREENER_ROWS} — truncated")
        ciks = sources.ticker_ciks()
        universe, no_cik = screen_universe(rows, ciks)
        counts["universe"] = len(universe)
        counts["excluded_no_cik"] = len(no_cik)
        _write_json(work, "universe.json", {"members": [m.__dict__ for m in universe], "excluded_no_cik": no_cik})
        if not universe:
            reasons.append("universe: the screen kept no names")
    except Exception as exc:
        reasons.append(f"universe: {type(exc).__name__}: {exc}")

    # Held picks ride along even after they leave the screen.
    held = sorted({normalize_symbol(s) for s in held_symbols})
    counts["held"] = len(held)
    companies: dict[str, str] = {m.symbol: m.cik for m in universe}
    held_failures: list[str] = []
    for symbol in held:
        if symbol in companies:
            continue
        cik = ciks.get(symbol)
        if cik is None:
            held_failures.append(f"{symbol} has no SEC CIK")
        else:
            companies[symbol] = cik
    if held_failures:
        reasons.append(_summarize("held picks", held_failures, len(held)))
    _write_json(work, "held.json", {"symbols": held})

    # Filings.
    cutoff_iso = now.astimezone(UTC).isoformat()
    filing_rows: list[dict[str, str]] = []
    sub_failures: list[str] = []
    doc_failures: list[str] = []
    attempted_docs = 0
    for symbol, cik in sorted(companies.items()):
        try:
            refs = select_filings(cik, sources.submissions(cik), since, cutoff_iso)
        except Exception as exc:
            sub_failures.append(f"{symbol}: {exc}")
            continue
        for ref in refs:
            attempted_docs += 1
            try:
                url = archive_url(cik, ref.accession, ref.primary_document)
                if ref.form == "8-K":
                    href = find_ex99_href(sources.filing_index(cik, ref.accession))
                    if href is not None:
                        url = archive_url(cik, ref.accession, href)
                text = excerpt(html_to_text(sources.document(url)), ref.form)
                name = f"filings/{symbol}_{ref.form}_{ref.accession}.txt"
                (work / name).write_text(text, encoding="utf-8")
                filing_rows.append(
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "form": ref.form,
                        "accession": ref.accession,
                        "filing_date": ref.filing_date,
                        "accepted_at": ref.accepted_at,
                        "url": url,
                        "excerpt_file": name,
                    }
                )
            except Exception as exc:
                doc_failures.append(f"{symbol} {ref.form} {ref.accession}: {exc}")
    counts["companies_checked"] = len(companies) - len(sub_failures)
    counts["filings"] = len(filing_rows)
    if sub_failures:
        reasons.append(_summarize("filings (submissions)", sub_failures, len(companies)))
    if doc_failures:
        reasons.append(_summarize("filings (documents)", doc_failures, attempted_docs))
    _write_json(work, "filings.json", {"since": since.isoformat(), "filings": filing_rows})

    # Prices: every member's last sale; history for the focus set.
    prices: dict[str, dict[str, object]] = {
        m.symbol: {"close": m.last_sale, "source": "screener", "date": as_of.isoformat()} for m in universe
    }
    focus = sorted(set(held) | {r["symbol"] for r in filing_rows} | set(BENCHMARK_SYMBOLS))
    price_failures: list[str] = []
    for symbol in focus:
        try:
            bars = sources.price_history(symbol)
        except Exception as exc:
            price_failures.append(f"{symbol}: {exc}")
            continue
        entry = prices.setdefault(symbol, {"close": bars[-1][1], "source": "history", "date": bars[-1][0]})
        entry["history"] = [list(b) for b in bars]
    counts["price_histories"] = len(focus) - len(price_failures)
    if price_failures:
        reasons.append(_summarize("prices", price_failures, len(focus)))
    _write_json(work, "prices.json", prices)

    status = SNAPSHOT_INCOMPLETE_STATUS if reasons else SNAPSHOT_COMPLETE_STATUS
    files = _content_files(work)
    content_hash = content_hash_of(files)
    _write_json(
        work,
        MANIFEST,
        {
            "id": snapshot_id,
            "kind": kind,
            "as_of": as_of.isoformat(),
            "created_at": created_at,
            "status": status,
            "reasons": reasons,
            "counts": counts,
            "content_hash": content_hash,
            "files": files,
            "screen": {
                "source": "Nasdaq stock screener download (api.nasdaq.com/api/screener/stocks)",
                "country": SCREEN_COUNTRY,
                "market_cap": [SCREEN_MIN_MARKET_CAP, SCREEN_MAX_MARKET_CAP],
                "min_price": SCREEN_MIN_PRICE,
                "min_dollar_volume": SCREEN_MIN_DOLLAR_VOLUME,
            },
            "filings_since": since.isoformat(),
        },
    )
    os.replace(work, final)
    return SnapshotResult(
        snapshot_id=snapshot_id,
        kind=kind,
        as_of=as_of.isoformat(),
        created_at=created_at,
        path=str(final),
        content_hash=content_hash,
        status=status,
        reasons=reasons,
        counts=counts,
    )


async def take_snapshot(
    session_maker: async_sessionmaker[AsyncSession],
    sources: SnapshotSources,
    *,
    kind: str,
    root: Path,
    now: datetime,
) -> SnapshotResult:
    """Read the held picks, build the folder, record the row. A crash that
    stops the folder from being written at all is still recorded, as an
    INCOMPLETE row with no hash — a missed snapshot is never silent."""
    from backend.research import picks_book_expected_shares, record_snapshot

    async with session_maker() as session:
        held = [s for s, q in (await picks_book_expected_shares(session)).items() if q > 0]
    try:
        result = await asyncio.to_thread(
            build_snapshot_folder, sources, kind=kind, root=root, now=now, held_symbols=held
        )
    except Exception as exc:
        result = SnapshotResult(
            snapshot_id=now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
            kind=kind,
            as_of=now.astimezone(MARKET_TZ).date().isoformat(),
            created_at=now.astimezone(UTC).isoformat(),
            path="",
            content_hash="",
            status=SNAPSHOT_INCOMPLETE_STATUS,
            reasons=[f"snapshot folder not written: {type(exc).__name__}: {exc}"],
        )
    async with session_maker() as session:
        await record_snapshot(
            session,
            snapshot_id=result.snapshot_id,
            kind=result.kind,
            as_of=result.as_of,
            created_at=result.created_at,
            path=result.path,
            content_hash=result.content_hash,
            status=result.status,
            reasons=result.reasons,
            counts=result.counts,
        )
    return result


def snapshot_root(arg: str | None) -> Path:
    raw = arg or os.environ.get(ROOT_VAR) or ""
    return Path(raw) if raw.strip() else DEFAULT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pixi run research-snapshot",
        description="Freeze the research brief's inputs into a timestamped folder (#1131). No AI runs here.",
    )
    parser.add_argument("--kind", choices=("nightly", "monthly"), default="nightly")
    parser.add_argument("--root", help=f"snapshot folder root (default ${ROOT_VAR}, else {DEFAULT_ROOT})")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    from backend.env import load_env

    try:
        load_env()
    except RuntimeError as exc:
        print(f"research-snapshot NOT RUN: {exc}", file=sys.stderr)
        return 2
    from backend.database import TRADING_MODE, async_session_maker, init_db

    if TRADING_MODE != "live":
        print(
            "research-snapshot NOT RUN: research records live in the live database — run it with the live overlay",
            file=sys.stderr,
        )
        return 2
    contact = (os.environ.get(SEC_CONTACT_VAR) or "").strip()
    if not contact:
        print(
            f"research-snapshot NOT RUN: {SEC_CONTACT_VAR} is unset — SEC requires a contact User-Agent",
            file=sys.stderr,
        )
        return 2

    async def _run() -> SnapshotResult:
        await init_db()
        return await take_snapshot(
            async_session_maker,
            HttpSources(contact),
            kind=args.kind.upper(),
            root=snapshot_root(args.root),
            now=datetime.now(UTC),
        )

    result = asyncio.run(_run())
    print(f"research-snapshot {result.snapshot_id} ({result.kind}, as of {result.as_of}): {result.status}")
    for key, value in sorted(result.counts.items()):
        print(f"  {key}: {value}")
    for reason in result.reasons:
        print(f"  INCOMPLETE: {reason}")
    return 0 if result.status == SNAPSHOT_COMPLETE_STATUS else 1


if __name__ == "__main__":
    sys.exit(main())
