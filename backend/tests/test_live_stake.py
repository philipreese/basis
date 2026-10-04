"""The private live stake (#1098): book_gates.resolve_for_book.

A live book's real-money stake lives in the gitignored `.env.live` overlay
(BASIS_LIVE_STAKE_<book id>), never in the public seeds.py. These tests pin:
paper never reads it; live reads only it; the stored config and its hash
never change because of it; a malformed or seeded stake refuses; and every
reader of a stored book's config goes through the one resolver (tripwire).
"""

import re
from pathlib import Path

import pytest

from backend import database
from backend.book_fingerprint import book_config_hash
from backend.book_gates import Envelope, live_stake_var, private_live_stake, resolve_for_book
from backend.live_executor import LiveRefusal, resolve_live_config
from backend.models import BookModel
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS

STAKE = 1234.0  # synthetic
B36_SEED = next(b for b in LAB_BOOKS if b["id"] == "B36")


def _b36() -> BookModel:
    return BookModel(id="B36", config=dict(B36_SEED["config"]), config_hash="h")


def test_paper_ignores_the_overlay_stake(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "paper")
    monkeypatch.setenv(live_stake_var("B36"), str(STAKE))
    config = resolve_for_book(_b36())
    assert config.stage1_stake is None
    assert config.envelope.basis == Envelope().basis  # the paper twin keeps its virtual basis


def test_paper_still_honours_a_seeded_paper_stake(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "paper")
    book = BookModel(id="B99", config={"envelope": {}, "stage1_stake": 500.0}, config_hash="h")
    assert resolve_for_book(book).stage1_stake == 500.0


def test_live_reads_the_overlay_stake_without_touching_the_stored_config(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "live")
    monkeypatch.setenv(live_stake_var("B36"), str(STAKE))
    book = _b36()
    before = dict(book.config)
    config = resolve_for_book(book)
    assert config.stage1_stake == STAKE and config.envelope.basis == STAKE
    assert book.config == before  # merged into a copy, never written back


def test_live_without_an_overlay_stake_is_unstaked(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "live")
    monkeypatch.delenv(live_stake_var("B36"), raising=False)
    assert resolve_for_book(_b36()).stage1_stake is None


def test_live_refuses_a_seeded_stake(monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "live")
    monkeypatch.setenv(live_stake_var("B36"), str(STAKE))
    book = BookModel(id="B36", config={**B36_SEED["config"], "stage1_stake": 500.0}, config_hash="h")
    with pytest.raises(ValueError, match="seeded stage1_stake in live mode"):
        resolve_for_book(book)


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "nan", "inf"])
def test_malformed_private_stake_raises_without_echoing_the_value(raw):
    with pytest.raises(ValueError) as refused:
        private_live_stake("B36", {live_stake_var("B36"): raw})
    assert raw not in str(refused.value)


def test_blank_private_stake_reads_as_unset():
    assert private_live_stake("B36", {live_stake_var("B36"): "  "}) is None
    assert private_live_stake("B36", {}) is None


def test_paper_b36_hash_is_unchanged_by_the_live_stake(monkeypatch):
    # The seed carries no stake, and the hash is computed from the seed
    # alone, so setting a private stake (in either mode) can never move the
    # paper twin's config_hash or restart its evidence era.
    assert "stage1_stake" not in B36_SEED["config"]
    baseline = book_config_hash(B36_SEED["config"], SEED_PLAYBOOKS)
    for mode in ("paper", "live"):
        monkeypatch.setattr(database, "TRADING_MODE", mode)
        monkeypatch.setenv(live_stake_var("B36"), str(STAKE))
        resolve_for_book(_b36())
        assert book_config_hash(B36_SEED["config"], SEED_PLAYBOOKS) == baseline


def test_no_seeded_book_carries_a_stake():
    # Paper rehearsal stakes are still legal on paper, but a seeded stake
    # would be refused in live mode and would publish a size. None today.
    assert [b["id"] for b in LAB_BOOKS if "stage1_stake" in b["config"]] == []


LIVE_ENV = {
    "IBKR_TRADING_MODE": "live",
    "IBKR_LIVE_ACCOUNT_ID": "U0000000",
    "IBKR_LIVE_GATEWAY_PORT": "4001",
    "IBKR_GATEWAY_PORT": "4001",
    "IBC_LIVE_START_SCRIPT": "C:/IBC/live.bat",
    "IBC_LIVE_INI": "C:/IBC/live/config.ini",
}
PAPER = {"IBKR_GATEWAY_PORT": "4002"}


def test_live_config_refuses_a_malformed_private_stake_up_front():
    env = {**LIVE_ENV, live_stake_var("B36"): "lots"}
    with pytest.raises(LiveRefusal, match="BASIS_LIVE_STAKE_B36 is not a number"):
        resolve_live_config(
            env,
            PAPER,
            overlay_in_use=True,
            dry_run=True,
            paper_view_of_overlay=env,
            overlay_values=env,
            arm_set_before_load=False,
        )
    ok = {**LIVE_ENV, live_stake_var("B36"): str(STAKE)}
    assert resolve_live_config(
        ok,
        PAPER,
        overlay_in_use=True,
        dry_run=True,
        paper_view_of_overlay=ok,
        overlay_values=ok,
        arm_set_before_load=False,
    )


# A reader that resolves a stored book's config without resolve_for_book
# would, in live mode, silently see no stake (#1098). The backtest replays
# seeds (paper only) and book_gates defines the resolver, so both are exempt.
_DIRECT = re.compile(r"resolve_book_config\(\s*\w+\.config\b")
_EXEMPT = {"book_gates.py"}


def test_every_stored_book_reader_goes_through_resolve_for_book():
    backend = Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(backend)}:{n}"
        for path in backend.rglob("*.py")
        if "tests" not in path.parts and "backtest" not in path.parts and path.name not in _EXEMPT
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _DIRECT.search(line)
    ]
    assert offenders == [], f"use book_gates.resolve_for_book(book) instead: {offenders}"
