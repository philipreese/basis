"""Tripwire for #897: forbid the deadline-first `while <clock-comparison>:` shape.

#884 fixed this inside wait_for_port; the same shape then re-entered twice
more (gateway_lifecycle.wait_for_gateway_port, broker.wait_for_terminal)
before #897 converted both to do-then-check-deadline. A `while` loop whose
OWN test reads a clock is deadline-first by construction — the guard is
evaluated before the body on every entry, including the first, so a
zero/degenerate budget skips the body (and its side effect/yield) entirely.
The only sanctioned shape is `while True: ...; if clock() >= deadline: break`,
which runs the body unconditionally at least once. A rare legitimate
exception can be marked with `# allow-deadline-first: <reason>` on the
`while` line.
"""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

CLOCK_CALL_NAMES = {"monotonic", "time"}
ALLOW_COMMENT = "allow-deadline-first"


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in CLOCK_CALL_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in CLOCK_CALL_NAMES
    return False


def _test_reads_the_clock(test: ast.expr) -> bool:
    return any(_is_clock_call(node) for node in ast.walk(test))


def _allowed(source_lines: list[str], lineno: int) -> bool:
    # The escape comment may sit on the while line itself or the line above.
    for check_line in (lineno, lineno - 1):
        if 1 <= check_line <= len(source_lines) and ALLOW_COMMENT in source_lines[check_line - 1]:
            return True
    return False


def _find_deadline_first_loops(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.While) and _test_reads_the_clock(node.test) and not _allowed(lines, node.lineno):
            findings.append(f"{path}:{node.lineno}")
    return findings


def test_no_while_loop_gates_on_the_clock_before_the_first_iteration():
    findings = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        findings.extend(_find_deadline_first_loops(path))
    assert not findings, (
        "deadline-first `while <clock-comparison>:` loop(s) found. Convert to "
        "do-then-check-deadline (`while True: ...; if clock() >= deadline: break`), "
        "matching wait_for_port's post-#884 shape, or mark a genuine exception with "
        f"a `# {ALLOW_COMMENT}: <reason>` comment on the while line. Offending sites:\n" + "\n".join(findings)
    )
