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
from backend.engine_revisions import CONSENSUS_VARIANTS, ENGINE_SOURCE_DIGEST
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS

BACKEND = Path(book_fingerprint.__file__).parent
ENGINE_MODULES = ("regime.py", "regime_variants.py")


def _hashes(playbooks: list[dict] = SEED_PLAYBOOKS) -> dict[str, str]:
    return {b["id"]: book_config_hash(b["config"], playbooks) for b in LAB_BOOKS}


def _moved(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {book_id for book_id in before if before[book_id] != after[book_id]}


def _engine_source_digest() -> str:
    """The engine modules' code with comments and docstrings ignored, so only
    an edit that could change a decision trips the pin."""
    parts = []
    for name in ENGINE_MODULES:
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
        b["id"] for b in LAB_BOOKS if not b["config"].get("playbook_ids") or target["id"] in b["config"]["playbook_ids"]
    }
    assert moved == selecting
    assert "B35" in moved  # whitelists it
    assert "B01" in moved  # no whitelist: can select every playbook
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


def test_a_regime_table_change_moves_every_book(monkeypatch):
    before = _hashes()
    table = dict(book_fingerprint.REGIME_ALLOWED_STRATEGIES)
    table["CALM_BULL"] = table["CALM_BULL"] - {"IRON_CONDOR"}
    monkeypatch.setattr(book_fingerprint, "REGIME_ALLOWED_STRATEGIES", table)
    assert _moved(before, _hashes()) == {b["id"] for b in LAB_BOOKS}
