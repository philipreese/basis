"""Tripwire (#835, extended repo-wide by #841): every ib_async order
constructed in backend/*.py or backend/backtest/*.py must carry an explicit
``tif`` keyword.

Why this is load-bearing and not style: IBKR emits the informational Error
10349 ("Order TIF was set to DAY based on order preset") for any order
submitted without an explicit time-in-force, and ib_async treats the unknown
code as fatal — it marks the Trade Cancelled while IBKR actually executes
the order. Observed live 2026-08-26: five flatten market orders all logged
Cancelled by ib_async, yet every one filled. A production order path that
omits ``tif`` would leave the executor believing nothing was placed while
the broker holds a live position — drift discovered a session late at best.
The whatIf variant of the same failure crashed the 2026-08-25 run (#826,
fixed by #828/#833).

Originally broker.py-only; #841 widened the scan to every module directly
under backend/ and backend/backtest/ (non-recursive; backend/tests/ is
deliberately excluded — fixtures there construct bare ib_async orders on
purpose) so a new order-construction site anywhere in production code (not
just the current broker adapter) trips this the moment it's added, and
taught it to resolve aliased imports (``from ib_async import LimitOrder as
X``) via AST import tracking instead of matching bare names.

AST-based like the #674 vocabulary tripwire: adding an order site without an
explicit tif fails HERE, naming the file:line — the failure is the review.
"""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (BACKEND_ROOT, BACKEND_ROOT / "backtest")

ORDER_CONSTRUCTORS = {"LimitOrder", "MarketOrder", "StopOrder", "StopLimitOrder", "Order"}


def _backend_py_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        files.extend(sorted(p for p in d.glob("*.py")))
    return files


def _ib_async_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local names to their real ib_async names for ``from ib_async
    import X as Y`` — so a call to ``Y(...)`` resolves to ``X`` for the
    ORDER_CONSTRUCTORS check below. Plain ``import ib_async as m`` needs no
    entry here: ``m.LimitOrder(...)`` is an Attribute whose ``.attr`` is
    already "LimitOrder", unaffected by the module-level alias."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ib_async":
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
    return aliases


def _constructor_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_every_backend_order_construction_sets_explicit_tif() -> None:
    offenders: list[str] = []
    for path in _backend_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _ib_async_aliases(tree)
        rel = path.relative_to(BACKEND_ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _constructor_name(node, aliases)
            if name not in ORDER_CONSTRUCTORS:
                continue
            if not any(kw.arg == "tif" for kw in node.keywords):
                offenders.append(f"{rel}:{node.lineno} {name}(...) has no explicit tif=")
    assert not offenders, (
        "Order construction(s) without an explicit tif keyword — IBKR's 10349 preset notice makes "
        "ib_async mark such orders Cancelled while they actually execute (#835):\n" + "\n".join(offenders)
    )


def test_the_tripwire_sees_the_known_order_sites() -> None:
    """Guard the guard: if the order sites are refactored away (or the scan
    is narrowed) so it finds nothing at all, the pin above would pass
    vacuously."""
    count = 0
    for path in _backend_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _ib_async_aliases(tree)
        count += sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Call) and _constructor_name(n, aliases) in ORDER_CONSTRUCTORS
        )
    assert count >= 4, f"expected at least the 4 known order sites (entry, TP child, close, preview), found {count}"
