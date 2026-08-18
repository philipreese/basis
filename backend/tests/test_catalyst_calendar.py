"""Tests for the seeded FOMC/CPI catalyst calendar (#131).

The EVENT_CATALYST regime, catalyst entry filters, and catalyst exit rules
all key off catalyst_dates — previously hand-typed. These pin the additive
idempotent merge, pruning, CPI's MAJOR classification, and the staleness
guard.
"""

import datetime

from backend.calendars import CPI_DATES, FOMC_DATES, stale_calendars
from backend.catalyst_calendar import merge_catalysts, seeded_catalysts
from backend.regime import parse_catalyst
from backend.regime_variants import major_catalyst_within

TODAY = datetime.date(2026, 8, 18)


class TestMerge:
    def test_merge_is_idempotent(self):
        once = merge_catalysts([], TODAY)
        assert merge_catalysts(once, TODAY) == once

    def test_manual_entries_survive_the_merge(self):
        manual = "EARNINGS:2026-09-03 NVDA"
        merged = merge_catalysts([manual], TODAY)
        assert manual in merged
        assert "FOMC:2026-09-16" in merged

    def test_undated_manual_notes_are_kept_verbatim(self):
        merged = merge_catalysts(["watch jackson hole"], TODAY)
        assert "watch jackson hole" in merged

    def test_long_past_entries_are_pruned(self):
        merged = merge_catalysts(["FOMC:2026-01-28", "MINOR:2026-08-01"], TODAY)
        assert "FOMC:2026-01-28" not in merged  # months past
        assert "MINOR:2026-08-01" in merged  # inside the 30-day grace

    def test_merged_list_is_date_sorted(self):
        merged = merge_catalysts([], TODAY)
        dated = [e.split(":")[1] for e in merged if ":" in e]
        assert dated == sorted(dated)


class TestClassification:
    def test_cpi_is_major(self):
        cat_type, active = parse_catalyst("CPI:2026-08-12", datetime.date(2026, 8, 10))
        assert cat_type == "MAJOR" and active

    def test_seeded_fomc_trips_the_v1_major_window(self):
        # 2026-09-16 FOMC: two trading days ahead of 2026-09-14.
        assert major_catalyst_within(merge_catalysts([], TODAY), datetime.date(2026, 9, 14))
        assert not major_catalyst_within(merge_catalysts([], TODAY), TODAY)


class TestPrefixedEntriesInTheScan:
    def test_entry_filter_window_reads_prefixed_dates(self):
        # Regression: _days_until assumed bare ISO strings and crashed (or
        # in observation.py silently never matched) on "FOMC:date" entries.
        from backend.eligibility import has_catalyst_within_14dte as _has_catalyst_within_14dte

        assert _has_catalyst_within_14dte(["FOMC:2026-09-16"], datetime.date(2026, 9, 10))
        assert not _has_catalyst_within_14dte(["FOMC:2026-09-16"], TODAY)
        assert not _has_catalyst_within_14dte(["watch jackson hole"], TODAY)

    def test_lifecycle_event_catalyst_conflict_reads_prefixed_dates(self):
        from backend.observation import run_lifecycle_scan
        from backend.tests.test_experiment_matrix import _position

        pos = _position(expiration="2026-09-18")
        result = run_lifecycle_scan(pos, "EVENT_CATALYST", 758.0, ["FOMC:2026-09-16"], today=TODAY)
        assert result["priority"] == "P2 — REVIEW"
        assert "catalyst" in result["reason"]


class TestCoverage:
    def test_seeded_calendar_covers_the_horizon_today(self):
        assert "FOMC/CPI catalysts" not in stale_calendars(TODAY)

    def test_staleness_fires_as_coverage_ends(self):
        last = max(datetime.date.fromisoformat(d) for d in (*FOMC_DATES, *CPI_DATES))
        assert "FOMC/CPI catalysts" in stale_calendars(last - datetime.timedelta(days=30))

    def test_seed_format_parses_as_dated(self):
        for entry in seeded_catalysts():
            cat_type, _ = parse_catalyst(entry, TODAY)
            assert cat_type == "MAJOR"
