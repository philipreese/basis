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
        assert config.exit_on_regime_flip is False  # default off (#254)

    def test_exit_on_regime_flip_resolves(self):
        config = resolve_book_config({"exit_on_regime_flip": True})
        assert config.exit_on_regime_flip is True

    def test_share_symbols_default_to_not_designated(self):
        assert resolve_book_config({}).share_symbols == ()

    def test_share_symbols_resolve(self):
        assert resolve_book_config({"share_symbols": ["VTI", "IEF"]}).share_symbols == ("VTI", "IEF")

    @pytest.mark.parametrize("bad", ["VTI", ["VTI", ""], ["VTI", 7]])
    def test_malformed_share_symbols_raise(self, bad):
        # A bare string would iterate into letters and designate "V", "T", "I".
        with pytest.raises(ValueError, match="share_symbols"):
            resolve_book_config({"share_symbols": bad})

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

    def test_b36_is_the_only_share_book_and_carries_the_ruled_menu(self):
        # #1054 operator ruling (menu swapped to low-priced equivalents by
        # #1087 so whole shares work at a small stake): six assets plus TBIL
        # as the cash leg, 10 months.
        share_books = [spec["id"] for spec in LAB_BOOKS if resolve_book_config(spec["config"]).is_share_book]
        assert share_books == ["B36"]
        (b36,) = [spec for spec in LAB_BOOKS if spec["id"] == "B36"]
        trend = resolve_book_config(b36["config"]).etf_trend
        assert trend is not None
        assert trend.menu == ("SCHB", "SCHF", "UTEN", "IAUM", "SCHH", "DBMF")
        assert (trend.cash_symbol, trend.trend_months) == ("TBIL", 10)

    def test_b36_menu_and_cash_symbol_are_fetchable(self):
        # #1087: a menu or cash-leg symbol the market-data layer doesn't know
        # about would route as a CBOE index (ETF_SYMBOLS) or never get its
        # month-end history backfilled (INDEX_SYMBOLS) — both fail soft, so
        # this must be a loud test, not a runtime surprise.
        from backend.market_data import ETF_SYMBOLS
        from backend.operator import INDEX_SYMBOLS

        (b36,) = [spec for spec in LAB_BOOKS if spec["id"] == "B36"]
        trend = resolve_book_config(b36["config"]).etf_trend
        assert trend is not None
        symbols = {*trend.menu, trend.cash_symbol}
        assert symbols <= set(ETF_SYMBOLS), symbols - set(ETF_SYMBOLS)
        assert symbols <= set(INDEX_SYMBOLS), symbols - set(INDEX_SYMBOLS)

    def test_b36_starts_halted_for_operator_enablement(self):
        (b36,) = [spec for spec in LAB_BOOKS if spec["id"] == "B36"]
        assert b36["initial_control"]["state"] == "HALT_ENTRIES"

    def test_b37_is_the_packaging_study_variant_as_replayed(self):
        # #1079: #1056's `condor-wide-far` was B01's config with exactly
        # these keys added (analysis/1056/packaging.py). A drift here races
        # a different book than the one the study failed to eliminate.
        (b37,) = [spec for spec in LAB_BOOKS if spec["id"] == "B37"]
        assert b37["config"] == {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {"max_loss_pct_per_trade": 10.0},
            "playbook_ids": ["spy_iron_condor_v1"],
            "playbook_overrides": {
                "execution_specs.spread_width_dollars": 10.0,
                "execution_specs.target_dte": 66,
                "exit_rules.mandatory_exit_dte": 21,
            },
        }
        assert resolve_book_config(b37["config"]).envelope.max_loss_pct_per_trade == 10.0
        assert b37["initial_control"]["state"] == "HALT_ENTRIES"


_TREND = {"menu": ["VTI", "IEF"], "cash_symbol": "SGOV", "trend_months": 10}


class TestEtfTrendConfig:
    def test_absent_means_an_options_book(self):
        assert resolve_book_config({}).etf_trend is None
        assert not resolve_book_config({}).is_share_book

    def test_resolves_when_symbols_match_the_designation(self):
        config = resolve_book_config({"share_symbols": ["VTI", "IEF", "SGOV"], "etf_trend": _TREND})
        assert config.is_share_book
        assert config.etf_trend is not None and config.etf_trend.menu == ("VTI", "IEF")

    @pytest.mark.parametrize(
        ("share_symbols", "trend", "match"),
        [
            (["VTI", "IEF"], _TREND, "must equal share_symbols"),  # cash leg not designated
            (["VTI", "IEF", "SGOV", "GLD"], _TREND, "must equal share_symbols"),  # extra designation
            (["VTI", "IEF", "SGOV"], {**_TREND, "menu": "VTI"}, "menu"),
            (["VTI", "IEF", "SGOV"], {**_TREND, "menu": []}, "menu"),
            (["VTI", "SGOV"], {**_TREND, "menu": ["VTI", "SGOV"]}, "cash_symbol"),
            (["VTI", "IEF", "SGOV"], {**_TREND, "trend_months": 1}, "trend_months"),
            (["VTI", "IEF", "SGOV"], {**_TREND, "trend_months": True}, "trend_months"),
            (["VTI", "IEF", "SGOV"], {**_TREND, "lookback": 3}, "Unknown etf_trend"),
            (["VTI", "SGOV"], {**_TREND, "menu": ["VTI", "VTI"]}, "must equal share_symbols"),
        ],
    )
    def test_malformed_trend_blocks_fail_loudly(self, share_symbols, trend, match):
        with pytest.raises(ValueError, match=match):
            resolve_book_config({"share_symbols": share_symbols, "etf_trend": trend})

    def test_non_mapping_trend_block_fails_loudly(self):
        with pytest.raises(TypeError, match="etf_trend"):
            resolve_book_config({"share_symbols": ["SGOV"], "etf_trend": ["VTI"]})
