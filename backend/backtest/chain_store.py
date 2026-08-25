"""Option-chain corpus store: optionsDX monthly txt files -> SQLite -> per-day snapshots.

ADR-0015 separation (spec/decisions.md, ADR-0015 §2): this module imports
NOTHING from backend.console, backend.evidence, or backend.database. It takes
explicit filesystem paths, opens its own sqlite3 connections, and never
touches the production DB. Backtest data lives entirely outside the evidence
ledger and the production data directory.

Storage is RAW (data honesty, ADR-0015 §4): crossed/locked quotes are stored
exactly as the vendor shipped them and only filtered to unquoted at LOAD time,
with the drop count reported on each snapshot (#793 declared assumption 1).

CLI: python -m backend.backtest.chain_store <txt_dir> <underlying> <db_path>
"""

from __future__ import annotations

import csv
import datetime
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Columns pulled from the optionsDX header (bracketed, space-padded).
_COLUMNS = (
    "QUOTE_DATE",
    "EXPIRE_DATE",
    "STRIKE",
    "C_BID",
    "C_ASK",
    "C_DELTA",
    "P_BID",
    "P_ASK",
    "P_DELTA",
    "UNDERLYING_LAST",
    "DTE",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chains (
    underlying TEXT NOT NULL,
    quote_date TEXT NOT NULL,
    expire_date TEXT NOT NULL,
    strike REAL NOT NULL,
    c_bid REAL,
    c_ask REAL,
    c_delta REAL,
    p_bid REAL,
    p_ask REAL,
    p_delta REAL,
    underlying_last REAL,
    dte REAL,
    PRIMARY KEY (underlying, quote_date, expire_date, strike)
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_chains_und_date ON chains (underlying, quote_date);"

#: SPY chains before this date are DEGRADED (4-11% crossed quotes, real
#: zero-bid holes in the tradeable band — #793) and excluded from
#: verdict-grade runs. SPX covers those years.
SPY_DEGRADED_BEFORE = datetime.date(2015, 1, 1)

#: XSP is not in the corpus; SPX proxies it at ÷10 scale (declared
#: assumption, ADR-0015 §4 / #792).
_XSP_SCALE = 10.0


class DegradedCorpusError(Exception):
    """Raised when a snapshot request falls in a degraded trust tier (#793)."""


@dataclass(frozen=True)
class BuildReport:
    """Outcome of one build_chain_db ingest pass."""

    rows_ingested: int
    rows_deduped: int
    dates_covered: list[str]


@dataclass(frozen=True)
class Quote:
    """One side of a chain row after the load-time unquoted filter.

    bid/ask are None when the stored side is unusable (empty, zero-bid,
    or dropped as crossed/locked).
    """

    bid: float | None
    ask: float | None
    delta: float | None


@dataclass(frozen=True)
class ChainSnapshot:
    """All quotes for one (underlying, quote_date), keyed (expire_iso, strike, right)."""

    quote_date: datetime.date
    underlying_last: float | None
    derived_from_spx: bool
    dropped_crossed: int
    quotes: dict[tuple[str, float, str], Quote] = field(default_factory=dict)


def _num(raw: str) -> float | None:
    """Parse a space-padded optionsDX numeric field; empty string -> None."""
    text = raw.strip()
    if not text:
        return None
    return float(text)


def _header_index(header: list[str]) -> dict[str, int]:
    """Map bare column names to positions from a bracketed, padded header row."""
    index: dict[str, int] = {}
    for pos, cell in enumerate(header):
        name = cell.strip().lstrip("[").rstrip("]").strip()
        index[name] = pos
    missing = [c for c in _COLUMNS if c not in index]
    if missing:
        raise ValueError(f"optionsDX header missing columns: {missing}")
    return index


def build_chain_db(txt_dir: Path, underlying: str, db_path: Path) -> BuildReport:
    """Ingest every *.txt in txt_dir into the chains table, RAW.

    Rows are stored exactly as shipped (crossed quotes kept as-is — the
    unquoted filter runs at load time, ADR-0015 §4); empty numeric fields
    become NULL. Duplicate (underlying, quote_date, expire_date, strike)
    keys keep the FIRST row seen (the corpus has overlapping archives).
    Idempotent per (underlying, quote_date): re-ingesting a month deletes
    and replaces its dates, never duplicates them.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(_INDEX)
        ingested = 0
        deduped = 0
        dates: set[str] = set()
        cleared: set[str] = set()
        cursor = conn.cursor()
        for txt_path in sorted(txt_dir.glob("*.txt")):
            with txt_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
                if header is None:
                    continue
                idx = _header_index(header)
                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    quote_date = row[idx["QUOTE_DATE"]].strip()
                    if quote_date not in cleared:
                        cursor.execute(
                            "DELETE FROM chains WHERE underlying = ? AND quote_date = ?",
                            (underlying, quote_date),
                        )
                        cleared.add(quote_date)
                    cursor.execute(
                        "INSERT OR IGNORE INTO chains VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            underlying,
                            quote_date,
                            row[idx["EXPIRE_DATE"]].strip(),
                            _num(row[idx["STRIKE"]]),
                            _num(row[idx["C_BID"]]),
                            _num(row[idx["C_ASK"]]),
                            _num(row[idx["C_DELTA"]]),
                            _num(row[idx["P_BID"]]),
                            _num(row[idx["P_ASK"]]),
                            _num(row[idx["P_DELTA"]]),
                            _num(row[idx["UNDERLYING_LAST"]]),
                            _num(row[idx["DTE"]]),
                        ),
                    )
                    if cursor.rowcount:
                        ingested += 1
                        dates.add(quote_date)
                    else:
                        deduped += 1
        conn.commit()
        return BuildReport(
            rows_ingested=ingested,
            rows_deduped=deduped,
            dates_covered=sorted(dates),
        )
    finally:
        conn.close()


def _filter_side(bid: float | None, ask: float | None) -> tuple[float | None, float | None, bool]:
    """Apply the #793 load-time unquoted filter to one side.

    Returns (bid, ask, was_crossed). A side with bid>0 and ask<=bid
    (crossed/locked) becomes fully unquoted — conservative direction:
    fewer tradable candidates, never a flattering fill. bid<=0 with a
    live ask is one-sided: keep the ask, null the bid.
    """
    if bid is not None and bid > 0 and ask is not None and ask <= bid:
        return None, None, True
    if (bid is None or bid <= 0) and ask is not None and ask > 0:
        return None, ask, False
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask <= 0:
        ask = None
    return bid, ask, False


class ChainStore:
    """Read-side of the chains DB: per-day snapshots with declared-rule filtering."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def snapshot(
        self,
        underlying: str,
        quote_date: datetime.date,
        *,
        allow_degraded: bool = False,
    ) -> ChainSnapshot | None:
        """Chain snapshot for one day, or None if the date is absent.

        A missing day is NO snapshot — never interpolate (#793 declared
        assumption 2: missing day = no-entry + mark-gap).

        Raises DegradedCorpusError for SPY before 2015-01-01 unless
        allow_degraded=True: #793 tiered SPY 2010-2014 as DEGRADED
        (crossed quotes and zero-bid holes in the tradeable band) and
        excluded it from verdict-grade runs; SPX covers those years.

        "XSP" is a derived view over stored SPX rows — strikes,
        underlying_last, and quote prices divided by 10, deltas unchanged
        — flagged derived_from_spx=True (declared assumption per
        ADR-0015 §4).
        """
        if underlying == "SPY" and quote_date < SPY_DEGRADED_BEFORE and not allow_degraded:
            raise DegradedCorpusError(
                f"SPY {quote_date.isoformat()} is in the degraded tier "
                f"(pre-{SPY_DEGRADED_BEFORE.isoformat()}, #793); "
                "pass allow_degraded=True for non-verdict-grade use"
            )
        derived = underlying == "XSP"
        stored = "SPX" if derived else underlying
        scale = _XSP_SCALE if derived else 1.0
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT expire_date, strike, c_bid, c_ask, c_delta, "
                "p_bid, p_ask, p_delta, underlying_last "
                "FROM chains WHERE underlying = ? AND quote_date = ?",
                (stored, quote_date.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        quotes: dict[tuple[str, float, str], Quote] = {}
        dropped = 0
        underlying_last: float | None = None
        for expire, strike, c_bid, c_ask, c_delta, p_bid, p_ask, p_delta, und in rows:
            if und is not None:
                underlying_last = und / scale
            strike_out = strike / scale
            for right, bid, ask, delta in (
                ("C", c_bid, c_ask, c_delta),
                ("P", p_bid, p_ask, p_delta),
            ):
                bid_s = bid / scale if bid is not None else None
                ask_s = ask / scale if ask is not None else None
                fbid, fask, crossed = _filter_side(bid_s, ask_s)
                if crossed:
                    dropped += 1
                quotes[(expire, strike_out, right)] = Quote(bid=fbid, ask=fask, delta=delta)
        return ChainSnapshot(
            quote_date=quote_date,
            underlying_last=underlying_last,
            derived_from_spx=derived,
            dropped_crossed=dropped,
            quotes=quotes,
        )

    def available_dates(self, underlying: str) -> list[datetime.date]:
        """Distinct quote dates stored for an underlying, ascending.

        XSP reports SPX's dates (it is a derived view over SPX rows).
        """
        stored = "SPX" if underlying == "XSP" else underlying
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT quote_date FROM chains WHERE underlying = ? ORDER BY quote_date",
                (stored,),
            ).fetchall()
        finally:
            conn.close()
        return [datetime.date.fromisoformat(r[0]) for r in rows]


def main(argv: list[str]) -> int:
    """CLI entry: build a chains DB from a directory of optionsDX txt files."""
    if len(argv) != 3:
        print(
            "usage: python -m backend.backtest.chain_store <txt_dir> <underlying> <db_path>",
            file=sys.stderr,
        )
        return 2
    report = build_chain_db(Path(argv[0]), argv[1], Path(argv[2]))
    first = report.dates_covered[0] if report.dates_covered else "-"
    last = report.dates_covered[-1] if report.dates_covered else "-"
    print(
        f"ingested={report.rows_ingested} deduped={report.rows_deduped} "
        f"dates={len(report.dates_covered)} range={first}..{last}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
