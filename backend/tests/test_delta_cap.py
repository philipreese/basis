"""Vol-aware short-delta cap (B33, #816 — the #814 disposition).

The load-bearing safety property is the GOLDEN PARITY class: with the knob
absent/None the entry pipeline's decisions are identical to pre-knob
behavior for every existing book — including the knob-off `vix_close or
20.0` fallback, which the knob path must refuse (fail closed, #814 F6) but
must never remove for knob-off books.

Scoping (#814 F4): the cap touches ONLY the short legs of credit
structures. Debit verticals' ~0.50Δ long legs and the long strangle's BUY
legs (which reuse `short_leg_delta`) pass through untouched at any VIX.
"""

import pytest

from backend.book_gates import resolve_book_config
from backend.models import PlaybookDefinitionSchema
from backend.opportunity import capped_playbooks, generate_trade_spec, scan_opportunities
from backend.seeds import LAB_BOOKS
from backend.tests.test_credit_spreads import TODAY
from backend.tests.test_opportunity import _make_market_state, _make_playbook, _make_portfolio_config


def _credit_playbook(short_delta: float = 0.16, strategy: str = "IRON_CONDOR") -> PlaybookDefinitionSchema:
    return _make_playbook(
        pb_id=f"cap_{strategy.lower()}",
        strategy=strategy,
        min_ivr=0.0,
        max_ivr=100.0,
        vix_min=0.0,
        vix_max=100.0,
        short_delta=short_delta,
    )


class TestKnobResolution:
    def test_default_is_off(self):
        assert resolve_book_config({}).delta_cap_vix is None
        assert resolve_book_config(None).delta_cap_vix is None

    def test_resolves_to_float(self):
        config = resolve_book_config({"delta_cap_vix": 4.5})
        assert config.delta_cap_vix == 4.5
        assert isinstance(config.delta_cap_vix, float)

    def test_b33_seed_carries_the_knob(self):
        (b33,) = [spec for spec in LAB_BOOKS if spec["id"] == "B33"]
        config = resolve_book_config(b33["config"])
        assert config.delta_cap_vix == 4.5
        assert config.underlying == "XSP" and config.variant == "V0"  # B01's config otherwise

    def test_b33_is_b01_plus_only_the_knob(self):
        """The arm's one-question discipline (ADR-0009): B33's config must be
        exactly B01's config plus delta_cap_vix — any other divergence is a
        confound."""
        (b01,) = [spec for spec in LAB_BOOKS if spec["id"] == "B01"]
        (b33,) = [spec for spec in LAB_BOOKS if spec["id"] == "B33"]
        without_knob = {k: v for k, v in b33["config"].items() if k != "delta_cap_vix"}
        assert without_knob == b01["config"]


class TestGoldenParity:
    """Knob absent/None ⇒ provably untouched existing behavior."""

    def test_no_preexisting_book_gained_the_knob(self):
        for spec in LAB_BOOKS:
            if spec["id"] == "B33":
                continue
            assert resolve_book_config(spec["config"]).delta_cap_vix is None, spec["id"]

    def test_inert_cap_is_identity_for_every_strategy(self):
        """When the cap doesn't bind, capped_playbooks returns the SAME
        playbook objects — not equal copies — so a scan over them is
        structurally the same scan. Combined with the executor-level spy
        (test_executor.TestDeltaCapBook: knob-off books never even reach
        capped_playbooks), this is the parity guarantee."""
        playbooks = [
            _credit_playbook(strategy=s)
            for s in ("IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "BROKEN_WING_BUTTERFLY")
        ] + [
            _make_playbook(pb_id=f"cap_{s.lower()}", strategy=s, min_ivr=0.0, vix_min=0.0, vix_max=100.0)
            for s in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_STRANGLE", "LONG_STRADDLE")
        ]
        result = capped_playbooks(playbooks, 4.5, 20.0)  # 0.225 cap > every 0.16 target
        assert all(a is b for a, b in zip(result, playbooks, strict=True))

    def test_knob_off_keeps_the_missing_vix_fallback(self):
        """F6 is scoped: knob-ON books sit out on missing VIX, but knob-off
        books keep the historical `vix_close or 20.0` fallback — a missing
        VIX still yields candidates, exactly as before this feature."""
        state = _make_market_state(ivr=55.0)  # above the income IVR gate
        state = state.model_copy(update={"vix_close": None})
        scan = scan_opportunities(
            playbooks=[_credit_playbook()],
            market_state=state,
            positions=[],
            portfolio_config=_make_portfolio_config(),
            today=TODAY,
            book_mode=True,
        )
        assert any(c.eligible for c in scan.candidates)


class TestCapMechanism:
    def test_cap_binds_at_high_vix(self):
        # target 0.16, cap 4.5, VIX 45 → 4.5/45 = 0.10 effective
        (capped,) = capped_playbooks([_credit_playbook(0.16)], 4.5, 45.0)
        assert capped.execution_specs.short_leg_delta == pytest.approx(0.10)

    def test_cap_inert_at_low_vix(self):
        # 4.5/20 = 0.225 > 0.16 → target unchanged; the playbook object
        # passes through IDENTICALLY (no copy, no re-round).
        pb = _credit_playbook(0.16)
        (result,) = capped_playbooks([pb], 4.5, 20.0)
        assert result is pb

    def test_capped_strikes_move_further_otm(self):
        pb = _credit_playbook(0.16)
        state = _make_market_state(vix=45.0)
        config = _make_portfolio_config()
        raw = generate_trade_spec(pb, state, [], config, today=TODAY).spec
        (capped_pb,) = capped_playbooks([pb], 4.5, 45.0)
        capped = generate_trade_spec(capped_pb, state, [], config, today=TODAY).spec
        assert raw is not None and capped is not None

        def short_strikes(spec):
            return {(leg.option_type, leg.action): leg.strike for leg in spec.legs if leg.action == "SELL"}

        assert capped.legs[0].strike < raw.legs[0].strike  # short put further below
        assert short_strikes(capped)[("CALL", "SELL")] > short_strikes(raw)[("CALL", "SELL")]

    @pytest.mark.parametrize("strategy", ["BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"])
    def test_debit_vertical_long_legs_untouched_at_any_vix(self, strategy):
        """#814 F4: the ~0.50Δ LONG legs of debit verticals must never be
        capped — that would convert near-ATM debit spreads into far-OTM
        lottery tickets. The playbook passes through by identity."""
        pb = _make_playbook(
            pb_id=f"cap_{strategy.lower()}",
            strategy=strategy,
            min_ivr=0.0,
            vix_min=0.0,
            vix_max=100.0,
            short_delta=0.16,
            long_delta=0.50,
        )
        for vix in (20.0, 45.0, 80.0):
            (result,) = capped_playbooks([pb], 4.5, vix)
            assert result is pb
        state = _make_market_state(vix=45.0)
        config = _make_portfolio_config()
        raw = generate_trade_spec(pb, state, [], config, today=TODAY).spec
        capped = generate_trade_spec(capped_playbooks([pb], 4.5, 45.0)[0], state, [], config, today=TODAY).spec
        assert raw is not None and capped is not None
        assert [leg.model_dump() for leg in capped.legs] == [leg.model_dump() for leg in raw.legs]

    def test_long_strangle_buy_legs_untouched(self):
        """The long strangle REUSES short_leg_delta for its two BUY legs —
        delta-name scoping would cap it wrongly; strategy-type scoping must
        leave it alone."""
        pb = _make_playbook(
            pb_id="cap_long_strangle",
            strategy="LONG_STRANGLE",
            min_ivr=0.0,
            vix_min=0.0,
            vix_max=100.0,
            short_delta=0.16,
        )
        (result,) = capped_playbooks([pb], 4.5, 60.0)
        assert result is pb


class TestFailClosed:
    @pytest.mark.parametrize("vix", [0.0, -1.0])
    def test_refuses_missing_or_sentinel_vix(self, vix):
        with pytest.raises(ValueError, match="real VIX close"):
            capped_playbooks([_credit_playbook()], 4.5, vix)

    def test_refuses_nonpositive_cap(self):
        with pytest.raises(ValueError, match="delta_cap_vix"):
            capped_playbooks([_credit_playbook()], 0.0, 20.0)
