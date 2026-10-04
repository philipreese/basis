"""A book's config_hash covers its behavior, not just its config (#1049).

The evidence era restarts only when config_hash changes, so anything that
decides how a book trades must move the hash: its playbooks, the engines it
reads, the regime table. These tests pin which books each kind of change
reaches, and that an engine-code edit can't land without a deliberate
decision about the revision table.
"""

import ast
import copy
import hashlib
from pathlib import Path

import pytest

from backend import book_fingerprint, engine_revisions
from backend.book_fingerprint import book_config_hash
from backend.book_gates import resolve_book_config
from backend.engine_revisions import (
    CONSENSUS_VARIANTS,
    ENGINE_SOURCE_DIGEST,
    ETF_TREND_SOURCE_DIGEST,
    TURN_OF_MONTH_SOURCE_DIGEST,
)
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS

BACKEND = Path(book_fingerprint.__file__).parent
ENGINE_MODULES = ("regime.py", "regime_variants.py")


def _hashes(playbooks: list[dict] = SEED_PLAYBOOKS) -> dict[str, str]:
    return {b["id"]: book_config_hash(b["config"], playbooks) for b in LAB_BOOKS}


def _moved(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {book_id for book_id in before if before[book_id] != after[book_id]}


def _engine_source_digest() -> str:
    return _source_digest(ENGINE_MODULES)


def _source_digest(modules: tuple[str, ...]) -> str:
    """The modules' code with comments and docstrings ignored, so only an
    edit that could change a decision trips the pin."""
    parts = []
    for name in modules:
        tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
        parts.append(ast.dump(tree, include_attributes=False))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def test_engine_code_change_requires_a_revision_decision():
    assert _engine_source_digest() == ENGINE_SOURCE_DIGEST, (
        "regime.py / regime_variants.py changed. If the change alters what any "
        "engine decides, bump that engine in ENGINE_REVISIONS "
        "(backend/engine_revisions.py) so its books start a fresh evidence era. "
        "Then set ENGINE_SOURCE_DIGEST to " + _engine_source_digest()
    )


def test_every_book_has_a_distinct_fingerprint():
    hashes = _hashes()
    assert len(set(hashes.values())) == len(hashes)


def test_a_playbook_change_moves_only_books_that_can_select_it():
    before = _hashes()
    playbooks = copy.deepcopy(SEED_PLAYBOOKS)
    target = next(pb for pb in playbooks if pb["id"] == "xsp_long_straddle_catalyst_v1")
    target["exit_rules"]["mandatory_exit_dte"] += 1
    moved = _moved(before, _hashes(playbooks))
    whitelisted = {b["id"] for b in LAB_BOOKS if b["config"].get("playbook_ids")}
    selecting = {
        b["id"]
        for b in LAB_BOOKS
        if not resolve_book_config(b["config"]).is_share_book  # #1054/#1092: a share book selects no playbook
        and (not b["config"].get("playbook_ids") or target["id"] in b["config"]["playbook_ids"])
    }
    assert moved == selecting
    assert "B35" in moved  # whitelists it
    assert "B01" in moved  # no whitelist: can select every playbook
    assert "B36" not in moved  # the ETF trend book reads no playbook at all
    assert "B38" not in moved  # the turn-of-month book reads no playbook at all
    assert moved.isdisjoint(whitelisted - {"B35"})


@pytest.mark.parametrize("variant", ["V0", "V2", "V3"])
def test_an_engine_bump_moves_its_books_and_the_consensus_books(variant, monkeypatch):
    before = _hashes()
    monkeypatch.setitem(engine_revisions.ENGINE_REVISIONS, variant, engine_revisions.ENGINE_REVISIONS[variant] + 1)
    moved = _moved(before, _hashes())
    expected = {
        b["id"]
        for b in LAB_BOOKS
        if b["config"].get("engine_variant") == variant
        or (b["config"].get("require_consensus") and variant in CONSENSUS_VARIANTS)
    }
    assert moved == expected
    assert expected, f"no seeded book reads {variant} — the parametrization is stale"


def test_designating_a_book_for_shares_moves_its_hash():
    # #1061: share_symbols decides what reconciliation accepts at the broker,
    # so designating a book is a behavior change and must restart its era.
    book = LAB_BOOKS[0]
    designated = {**book["config"], "share_symbols": ["VTI"]}
    assert book_config_hash(designated, SEED_PLAYBOOKS) != book_config_hash(book["config"], SEED_PLAYBOOKS)


def test_only_the_two_share_books_are_designated_for_shares():
    # #1061 shipped the plumbing with no designated book; #1054 designated
    # the monthly ETF trend book; #1092 (the operator ruling on #1082 sanity
    # check 6) designates the turn-of-month book, the second and so far last.
    assert [b["id"] for b in LAB_BOOKS if "share_symbols" in b["config"]] == ["B36", "B38"]


def test_a_regime_table_change_moves_every_options_book(monkeypatch):
    before = _hashes()
    table = dict(book_fingerprint.REGIME_ALLOWED_STRATEGIES)
    table["CALM_BULL"] = table["CALM_BULL"] - {"IRON_CONDOR"}
    monkeypatch.setattr(book_fingerprint, "REGIME_ALLOWED_STRATEGIES", table)
    assert _moved(before, _hashes()) == {
        b["id"] for b in LAB_BOOKS if not resolve_book_config(b["config"]).is_share_book
    }


def test_etf_trend_code_change_requires_a_revision_decision():
    # #1054: the trend rules are to B36 what an engine is to an options book.
    assert _source_digest(("etf_trend.py",)) == ETF_TREND_SOURCE_DIGEST, (
        "backend/etf_trend.py changed. If the change alters a signal, a target or an order, bump "
        "ETF_TREND_REVISION (backend/engine_revisions.py) so the share book starts a fresh evidence era. "
        "Then set ETF_TREND_SOURCE_DIGEST to " + _source_digest(("etf_trend.py",))
    )


def test_an_etf_trend_revision_bump_moves_both_share_books(monkeypatch):
    # #1092: a turn-of-month book's hash also reads ETF_TREND_REVISION (it
    # calls etf_trend.rebalance_orders/buy_limit/sell_limit for its orders).
    before = _hashes()
    monkeypatch.setattr(book_fingerprint, "ETF_TREND_REVISION", engine_revisions.ETF_TREND_REVISION + 1)
    assert _moved(before, _hashes()) == {"B36", "B38"}


def test_turn_of_month_code_change_requires_a_revision_decision():
    assert _source_digest(("turn_of_month.py",)) == TURN_OF_MONTH_SOURCE_DIGEST, (
        "backend/turn_of_month.py changed. If the change alters a signal, a target or an order, bump "
        "TURN_OF_MONTH_REVISION (backend/engine_revisions.py) so the share book starts a fresh evidence era. "
        "Then set TURN_OF_MONTH_SOURCE_DIGEST to " + _source_digest(("turn_of_month.py",))
    )


def test_a_turn_of_month_revision_bump_moves_only_b38(monkeypatch):
    before = _hashes()
    monkeypatch.setattr(book_fingerprint, "TURN_OF_MONTH_REVISION", engine_revisions.TURN_OF_MONTH_REVISION + 1)
    assert _moved(before, _hashes()) == {"B38"}


def test_the_turn_of_month_symbols_are_part_of_the_share_books_hash():
    b38 = next(b for b in LAB_BOOKS if b["id"] == "B38")["config"]
    swapped = {
        **b38,
        "turn_of_month": {"risk_symbol": "TBIL", "cash_symbol": "SCHB"},
        "share_symbols": ["TBIL", "SCHB"],
    }
    assert book_config_hash(swapped, SEED_PLAYBOOKS) != book_config_hash(b38, SEED_PLAYBOOKS)


def test_the_trend_parameters_are_part_of_the_share_books_hash():
    b36 = next(b for b in LAB_BOOKS if b["id"] == "B36")["config"]
    longer = {**b36, "etf_trend": {**b36["etf_trend"], "trend_months": 12}}
    assert book_config_hash(longer, SEED_PLAYBOOKS) != book_config_hash(b36, SEED_PLAYBOOKS)
