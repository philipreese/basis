"""Tripwire (#835): every ib_async order constructed in backend/broker.py
must carry an explicit ``tif`` keyword.

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

AST-based like the #674 vocabulary tripwire: adding an order site without an
explicit tif fails HERE, naming the file:line — the failure is the review.
"""

import ast
from pathlib import Path

BROKER = Path(__file__).resolve().parents[1] / "broker.py"

ORDER_CONSTRUCTORS = {"LimitOrder", "MarketOrder", "StopOrder", "StopLimitOrder", "Order"}


def _constructor_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_every_broker_order_construction_sets_explicit_tif() -> None:
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _constructor_name(node)
        if name not in ORDER_CONSTRUCTORS:
            continue
        if not any(kw.arg == "tif" for kw in node.keywords):
            offenders.append(f"backend/broker.py:{node.lineno} {name}(...) has no explicit tif=")
    assert not offenders, (
        "Order construction(s) without an explicit tif keyword — IBKR's 10349 preset notice makes "
        "ib_async mark such orders Cancelled while they actually execute (#835):\n" + "\n".join(offenders)
    )


def test_the_tripwire_sees_the_known_order_sites() -> None:
    """Guard the guard: if broker.py is refactored so the AST scan finds no
    order constructions at all, the pin above would pass vacuously."""
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and _constructor_name(n) in ORDER_CONSTRUCTORS)
    assert count >= 4, f"expected at least the 4 known order sites (entry, TP child, close, preview), found {count}"
