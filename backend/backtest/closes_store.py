"""Daily-close corpus store: per-symbol CSVs (#794) -> as-of-date sliced series.

ADR-0015 separation (spec/decisions.md, ADR-0015 §2): this module imports
NOTHING from backend.console, backend.evidence, or backend.database. It takes
an explicit directory path and never touches the production DB.

Look-ahead is the cardinal sin of replay: every read here is bounded by an
explicit `through` date and never returns a row dated after it.
"""

from __future__ import annotations

import csv
import datetime
from pathlib import Path


class ClosesStore:
    """Reads <symbol>.csv files: two columns date,close — ISO dates, oldest-first."""

    def __init__(self, dir_path: Path) -> None:
        self._dir_path = dir_path

    def daily_closes(self, symbol: str, *, through: datetime.date, days: int) -> list[tuple[str, float]] | None:
        """Trailing daily closes as of `through`, or None if the symbol is missing.

        Mirrors the shape and semantics of the production
        market_data.fetch_index_daily_closes: an oldest-first list of
        (iso_date, close) tuples, at most the trailing `days` entries,
        None when the series cannot be served (here: no <symbol>.csv).

        Only rows dated <= `through` are ever returned — look-ahead is the
        cardinal sin of replay, and this reader is the choke point that
        makes it structurally impossible.
        """
        path = self._dir_path / f"{symbol}.csv"
        if not path.is_file():
            return None
        cutoff = through.isoformat()
        rows: list[tuple[str, float]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for record in csv.reader(handle):
                if len(record) < 2:
                    continue
                date_text = record[0].strip()
                try:
                    close = float(record[1])
                except ValueError:
                    continue  # header row or malformed line
                if date_text <= cutoff:
                    rows.append((date_text, close))
        return rows[-days:]

    def latest_close(self, symbol: str, through: datetime.date) -> tuple[str, float] | None:
        """Most recent (iso_date, close) at or before `through`, or None."""
        rows = self.daily_closes(symbol, through=through, days=1)
        if not rows:
            return None
        return rows[-1]
