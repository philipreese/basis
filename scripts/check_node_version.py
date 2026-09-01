"""Tripwire for #937: the pixi env's Node major must match CI's.

CI (`.github/workflows/ci.yml`) pins Node via `actions/setup-node`, entirely
separate from the `nodejs` conda package `pixi.toml` resolves for local dev.
Nothing keeps those two pins in sync except this check — bump one without the
other and vitest's worker pool silently breaks in whichever environment
didn't move (#937: an unbounded local pin resolved Node 26 and crashed
tinypool while CI's Node 20 stayed green).

    pixi run node-version-check
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def ci_node_major() -> int:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"node-version:\s*['\"]?(\d+)", text)
    if not match:
        raise SystemExit(f"could not find 'node-version:' in {CI_WORKFLOW}")
    return int(match.group(1))


def pixi_node_major() -> int:
    result = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, check=True
    )
    match = re.match(r"v(\d+)", result.stdout.strip())
    if not match:
        raise SystemExit(f"could not parse 'node --version' output: {result.stdout!r}")
    return int(match.group(1))


def main() -> int:
    ci_major = ci_node_major()
    env_major = pixi_node_major()
    if ci_major != env_major:
        print(
            f"[-] Node version drift: pixi env resolves Node {env_major}.x, "
            f"but {CI_WORKFLOW} pins Node {ci_major}.x. "
            f"Bound pixi.toml's `nodejs` dependency to match and re-run `pixi install`.",
            file=sys.stderr,
        )
        return 1
    print(f"[+] Node version check passed (pixi env and CI both Node {ci_major}.x).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
