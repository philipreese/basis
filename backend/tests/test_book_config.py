"""Tests for the typed BookConfig resolution (backend/book_gates.py, #167).

resolve_book_config is the single seam through which every module reads a
book's config dict. The strictness contract: an unknown envelope key raises,
so a typo in a seeded book fails these tests instead of silently merging.
"""

import pytest

from backend.book_gates import BookConfig, Envelope, resolve_book_config
from backend.seeds import LAB_BOOKS


class TestResolve:
    def test_empty_config_yields_defaults(self):
        config = resolve_book_config(None)
        assert config == BookConfig()
        assert config.envelope == Envelope()
        assert config.envelope.basis == 10_000.0
        assert config.envelope.max_positions == 8

    def test_envelope_override_replaces_only_named_fields(self):
        config = resolve_book_config({"envelope": {"max_loss_pct_per_trade": 4.0}})
        assert config.envelope.max_loss_pct_per_trade == 4.0
        assert config.envelope.basis == Envelope().basis  # untouched default

    def test_numeric_coercion(self):
        config = resolve_book_config({"envelope": {"basis": 12_000, "max_positions": 4.0}})
        assert config.envelope.basis == 12_000.0
        assert isinstance(config.envelope.basis, float)
        assert config.envelope.max_positions == 4
        assert isinstance(config.envelope.max_positions, int)

    def test_unknown_envelope_key_raises(self):
        with pytest.raises(ValueError, match="max_positons"):
            resolve_book_config({"envelope": {"max_positons": 4}})

    def test_book_fields_resolve(self):
        config = resolve_book_config(
            {
                "engine_variant": "V3",
                "underlying": "XSP",
                "ignore_regime": True,
                "playbook_ids": ["spy_calendar_spread_v1"],
                "playbook_overrides": {"enabled": True},
            }
        )
        assert config.variant == "V3"
        assert config.underlying == "XSP"
        assert config.ignore_regime is True
        assert config.ignore_ivr is False
        assert config.playbook_ids == ("spy_calendar_spread_v1",)
        assert config.playbook_overrides == {"enabled": True}

    def test_missing_variant_and_underlying_stay_none(self):
        config = resolve_book_config({})
        assert config.variant is None
        assert config.underlying is None
        assert config.playbook_ids is None

    def test_unknown_top_level_keys_are_permissive(self):
        # B00-legacy configs predate the typed fields; only envelope is strict.
        config = resolve_book_config({"legacy_field": "whatever"})
        assert config == BookConfig()


class TestSeededBooksResolve:
    def test_every_lab_book_resolves(self):
        """The loud-failure guarantee: a typo in any seeded book's envelope
        breaks this test at commit time, not silently in production."""
        for spec in LAB_BOOKS:
            config = resolve_book_config(spec["config"])
            assert config.envelope.basis > 0, spec["id"]

    def test_b21_carries_its_widened_envelope(self):
        (b21,) = [spec for spec in LAB_BOOKS if spec["id"] == "B21"]
        assert resolve_book_config(b21["config"]).envelope.max_loss_pct_per_trade == 4.0
