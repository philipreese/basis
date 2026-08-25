"""Minimum-credit floor (B34, #820 — #818 backlog item 1).

The load-bearing safety property is the GOLDEN PARITY class: with the knob
absent/None the entry pipeline's decisions are identical to pre-knob
behavior for every existing book — the floor is a pure refusal gate that
adds no fallback, fabricates no quote, and never touches a knob-off book.

Scoping: CREDIT structures only (`spec.premium_direction == "CREDIT"`, the
single provenance every quote gate in _try_place_entry already reads, #621).
Debit entries pay their |net_mid| as max loss — "thin" is not a hazard
there — and a zero width_bound (calendars, straddles/strangles: no
same-type multi-strike span) leaves the floor inert: no denominator.

Executor-level behavior tests (thin refused / thick placed / debit and
zero-width untouched / knob-off parity) live in
test_executor.TestMinCreditFloor beside the other _try_place_entry quote
gates; the replay mirror's tests live in test_backtest_engine.
"""

from backend.anomaly import _REJECTION_EVENTS
from backend.book_gates import resolve_book_config
from backend.seeds import LAB_BOOKS


class TestKnobResolution:
    def test_default_is_off(self) -> None:
        assert resolve_book_config({}).min_credit_ratio is None
        assert resolve_book_config(None).min_credit_ratio is None

    def test_resolves_to_float(self) -> None:
        config = resolve_book_config({"min_credit_ratio": 0.15})
        assert config.min_credit_ratio == 0.15
        assert isinstance(config.min_credit_ratio, float)

    def test_b34_seed_carries_the_knob(self) -> None:
        (b34,) = [spec for spec in LAB_BOOKS if spec["id"] == "B34"]
        config = resolve_book_config(b34["config"])
        assert config.min_credit_ratio == 0.15
        assert config.underlying == "XSP" and config.variant == "V0"  # B01's config otherwise

    def test_b34_is_b01_plus_only_the_knob(self) -> None:
        """The arm's one-question discipline (ADR-0009): B34's config must be
        exactly B01's config plus min_credit_ratio — any other divergence is
        a confound."""
        (b01,) = [spec for spec in LAB_BOOKS if spec["id"] == "B01"]
        (b34,) = [spec for spec in LAB_BOOKS if spec["id"] == "B34"]
        without_knob = {k: v for k, v in b34["config"].items() if k != "min_credit_ratio"}
        assert without_knob == b01["config"]


class TestGoldenParity:
    def test_no_preexisting_book_gained_the_knob(self) -> None:
        for spec in LAB_BOOKS:
            if spec["id"] == "B34":
                continue
            assert resolve_book_config(spec["config"]).min_credit_ratio is None, spec["id"]


class TestAnomalyPosture:
    def test_thin_credit_refusal_is_not_a_rejection_event(self) -> None:
        """#820: ENTRY_REFUSED_THIN_CREDIT is a QUALITY gate on our own
        decision, not a rejection-shaped failure — a deliberately strict
        knob refusing thin credits every night is the knob working, and must
        never trip REPEATED_REJECTION. Pinned so a future edit that pools it
        with ORDER_REJECTED/ENTRY_PREVIEW_REFUSED has to argue with this
        test, not just grow the tuple."""
        assert "ENTRY_REFUSED_THIN_CREDIT" not in _REJECTION_EVENTS
