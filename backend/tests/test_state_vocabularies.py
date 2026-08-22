"""Tripwire: raw status-literal enumerations in query predicates (#674).

AGENTS.md's state-enumeration review rule (#671) is diligence-shaped — it
relies on a reader finding every existing predicate by hand. The cross-book
netting gate reasoned over OPEN positions from an era when open positions
WERE the account's whole broker-visible exposure, and silently missed
STAGED/SUBMITTED/PARTIAL orders as real exposure once those existed (#665).
backend/states.py centralizes the vocabularies; THIS test is the actual
enforcement — a query predicate that spells out a raw status string instead
of importing a named set from backend/states.py fails here, naming the
offending file:line, rather than waiting for the next external audit.

Scope, deliberately narrow: SQLAlchemy query-predicate calls
(`.filter(...)`, `.filter_by(...)`, `.where(...)`) that compare a
`<Model>.status` class attribute against a raw string literal, or call
`.status.in_(...)` on a literal tuple/list/set. Plain Python checks against
an already-loaded ORM instance (`order.status == "PARTIAL"` inside a
list comprehension or `if`) are a different, much broader category outside
this issue's scope — they carry no "silently outgrown enumeration" risk the
same way, since there's no growable predicate to miss a new member of.

A deliberately narrow literal predicate stays expressible: append
`# state-literal-ok: <reason>` to the line and the tripwire skips it.
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ESCAPE_TOKEN = "state-literal-ok:"

# The vocabulary module itself defines these literals — that's the point.
EXEMPT_FILES = {"states.py"}


class _Violation(str):
    """A file:line:detail string — subclassing str keeps assertion output
    readable (pytest prints failed list members directly)."""


def _has_escape_comment(source_lines: list[str], lineno: int, end_lineno: int) -> bool:
    # Comments legitimately live on any line the (possibly multi-line) call
    # spans — check the whole range, 1-indexed like ast.lineno.
    for i in range(lineno, end_lineno + 1):
        if i - 1 < len(source_lines) and ESCAPE_TOKEN in source_lines[i - 1]:
            return True
    return False


def _is_status_attribute(node: ast.expr) -> bool:
    """True for `<Name>.status` — e.g. `OrderModel.status`, `PositionModel.status`."""
    return isinstance(node, ast.Attribute) and node.attr == "status" and isinstance(node.value, ast.Name)


def _literal_str_container(node: ast.expr) -> bool:
    """True if *node* is a literal tuple/list/set whose elements are all
    string constants — the raw-enumeration shape `.in_(("STAGED", ...))`,
    as opposed to `.in_(ORDER_PENDING_STATUSES)` (a Name reference, exempt)."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    return bool(node.elts) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)


def find_raw_status_literals(source: str, filename: str) -> list[_Violation]:
    """Scan *source* for query-predicate status-literal enumerations. Returns
    one violation string per offending call site."""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    violations: list[_Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr

        if method in ("filter", "where"):
            for arg in node.args:
                # `<Model>.status == "LITERAL"` (either operand order)
                if isinstance(arg, ast.Compare) and len(arg.ops) == 1 and isinstance(arg.ops[0], ast.Eq):
                    left, right = arg.left, arg.comparators[0]
                    literal_side = None
                    if _is_status_attribute(left) and isinstance(right, ast.Constant) and isinstance(right.value, str):
                        literal_side = right
                    elif _is_status_attribute(right) and isinstance(left, ast.Constant) and isinstance(left.value, str):
                        literal_side = left
                    if literal_side is not None and not _has_escape_comment(lines, node.lineno, node.end_lineno):
                        violations.append(
                            _Violation(
                                f"{filename}:{node.lineno}: .{method}(...status == {literal_side.value!r}...) "
                                "— import a named set from backend/states.py, or add "
                                f"'# {ESCAPE_TOKEN} <reason>'"
                            )
                        )
                # `<Model>.status.in_((...))` as a filter/where argument is
                # caught separately below (ast.walk visits that nested Call
                # node too, regardless of the parent) — not duplicated here.

        elif method == "filter_by":
            for kw in node.keywords:
                if (
                    kw.arg == "status"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and not _has_escape_comment(lines, node.lineno, node.end_lineno)
                ):
                    violations.append(
                        _Violation(
                            f"{filename}:{node.lineno}: .filter_by(status={kw.value.value!r}...) "
                            "— import a named set from backend/states.py, or add "
                            f"'# {ESCAPE_TOKEN} <reason>'"
                        )
                    )

        # `<Model>.status.in_((...))` — ast.walk visits this Call node on
        # its own regardless of whether it's nested as a filter()/where()
        # argument or used bare, so one branch catches both shapes.
        elif (
            method == "in_"
            and _is_status_attribute(node.func.value)
            and node.args
            and _literal_str_container(node.args[0])
            and not _has_escape_comment(lines, node.lineno, node.end_lineno)
        ):
            violations.append(
                _Violation(
                    f"{filename}:{node.lineno}: status.in_(<literal>) "
                    "— import a named set from backend/states.py, or add "
                    f"'# {ESCAPE_TOKEN} <reason>'"
                )
            )

    return violations


class TestNoRawStatusLiteralsOutsideVocabularyModule:
    def test_backend_modules_import_named_state_sets(self):
        violations: list[_Violation] = []
        for path in sorted(BACKEND_DIR.glob("*.py")):
            if path.name in EXEMPT_FILES:
                continue
            source = path.read_text(encoding="utf-8")
            violations.extend(find_raw_status_literals(source, f"backend/{path.name}"))
        assert not violations, (
            "Raw status-literal query predicates found outside backend/states.py "
            "(#674 tripwire — the AGENTS.md state-enumeration rule made mechanical):\n" + "\n".join(violations)
        )


class TestTripwireCatchesWhatItClaimsTo:
    """The tripwire scanner itself, proven against synthetic snippets — a
    passing suite above is worthless if this scanner is vacuously blind."""

    def test_catches_filter_by_literal(self):
        src = 'select(PositionModel).filter_by(status="OPEN")\n'
        violations = find_raw_status_literals(src, "synthetic.py")
        assert len(violations) == 1
        assert "OPEN" in violations[0]

    def test_catches_filter_equality_literal(self):
        src = 'select(BookModel).filter(BookModel.status == "ACTIVE")\n'
        violations = find_raw_status_literals(src, "synthetic.py")
        assert len(violations) == 1

    def test_catches_reversed_equality_operand_order(self):
        src = 'select(BookModel).filter("ACTIVE" == BookModel.status)\n'
        violations = find_raw_status_literals(src, "synthetic.py")
        assert len(violations) == 1

    def test_catches_where_in_literal_tuple(self):
        src = 'update(OrderModel).where(OrderModel.status.in_(("STAGED", "SUBMITTED")))\n'
        violations = find_raw_status_literals(src, "synthetic.py")
        assert len(violations) == 1

    def test_does_not_flag_a_named_set_reference(self):
        src = "select(OrderModel).filter(OrderModel.status.in_(ORDER_PENDING_STATUSES))\n"
        assert find_raw_status_literals(src, "synthetic.py") == []

    def test_does_not_flag_a_named_constant_equality(self):
        src = "select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS)\n"
        assert find_raw_status_literals(src, "synthetic.py") == []

    def test_escape_comment_on_the_call_line_suppresses_the_flag(self):
        src = 'select(PositionModel).filter_by(status="OPEN")  # state-literal-ok: exactly OPEN, on purpose\n'
        assert find_raw_status_literals(src, "synthetic.py") == []

    def test_escape_comment_on_a_later_line_of_a_multiline_call_suppresses_the_flag(self):
        src = 'select(PositionModel).filter_by(\n    status="OPEN"  # state-literal-ok: exactly OPEN, on purpose\n)\n'
        assert find_raw_status_literals(src, "synthetic.py") == []

    def test_does_not_flag_non_status_literal_predicates(self):
        # A literal on a DIFFERENT field (e.g. .filter_by(id="B01")) is not
        # this tripwire's concern.
        src = 'select(BookModel).filter_by(id="B01")\n'
        assert find_raw_status_literals(src, "synthetic.py") == []

    def test_does_not_flag_plain_python_instance_checks(self):
        # Out of scope by design (see module docstring): an already-loaded
        # ORM instance's attribute, not a query predicate.
        src = '[p for p in positions if p.status == "OPEN"]\n'
        assert find_raw_status_literals(src, "synthetic.py") == []
