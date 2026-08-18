"""Tests for the TLT rate-vol diversifier book B22 (#135).

SPY-derived regimes are blind to bonds, so B22 runs regime-agnostic with
the RV-rank pseudo-IVR gate as its selection discipline. TLT pays monthly
dividends: every ~38-DTE window spans an ex-date, so the #130 defense
keeps the book put-side by construction — pinned here.
"""

import datetime

from backend.assignment_defense import entry_ex_div_block
from backend.operator import INDEX_SYMBOLS
from backend.seeds import LAB_BOOKS

TODAY = datetime.date(2026, 8, 18)


class TestB22Seed:
    def test_b22_is_regime_agnostic_tlt(self):
        b22 = next(spec for spec in LAB_BOOKS if spec["id"] == "B22")
        assert b22["config"]["underlying"] == "TLT"
        assert b22["config"]["ignore_regime"] is True

    def test_tlt_closes_are_ingested_for_telemetry(self):
        assert "TLT" in INDEX_SYMBOLS


class TestMonthlyPayerConsequence:
    def test_any_38_dte_short_call_window_spans_an_ex_date(self):
        # Monthly dividends: from any date in calendar coverage, a 38-day
        # window contains an ex-date — call-side entries are always blocked.
        for offset in range(0, 360, 17):
            today = TODAY + datetime.timedelta(days=offset)
            expiration = today + datetime.timedelta(days=38)
            if expiration > datetime.date(2027, 12, 1):
                break
            assert entry_ex_div_block("TLT", has_short_call=True, today=today, expiration=expiration) is not None

    def test_put_side_stays_open(self):
        assert (
            entry_ex_div_block("TLT", has_short_call=False, today=TODAY, expiration=TODAY + datetime.timedelta(days=38))
            is None
        )
