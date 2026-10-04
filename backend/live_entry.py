"""live_entry.py — the live process's one entry point (#1065).

It exists for a single ordering reason: the live environment overlay
(backend/env.py, BASIS_ENV_OVERLAY) must be loaded BEFORE any module that
captures the environment at import time — backend.database reads
IBKR_TRADING_MODE once, on import, to pick the database file. So this module
imports nothing from the backend except env.py until load_env() has run; the
CLI itself lives in backend/live_cli.py.

    pixi run live-executor [--dry-run] [--rehearse]   # against a running live Gateway
    pixi run live-executor-nightly                     # the scheduled task: run + DB backup (Gateway stays up)
    pixi run live-gateway-check                        # is the persistent live Gateway logged in?
    pixi run live-grant grant --book B36 --attest "..."
"""

import sys

from backend.env import load_env


def main(argv: list[str] | None = None) -> int:
    try:
        load_env()
    except RuntimeError as exc:
        print(f"basis LIVE NOT RUN: {exc}", file=sys.stderr)
        return 2
    from backend.live_cli import dispatch

    return dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
