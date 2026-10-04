"""env.py — how a process loads its environment (#1065).

Every headless entrypoint and the console load the repo's `.env` with
override=True: the file wins over the process environment. That is right for
the paper lab, and it is also why a live process cannot be started by setting
IBKR_TRADING_MODE=live on the scheduled task alone — `.env` would put the
paper values straight back the moment backend.operator is imported.

So a live process names an OVERLAY file in BASIS_ENV_OVERLAY (the live pixi
tasks set it to `.env.live`). The overlay is loaded after `.env`, also with
override=True, so every name it sets wins — the mode, the Gateway port, the
start script — and names it leaves out fall through to `.env`. BASIS_ENV_OVERLAY
itself never appears in either file, so `.env` cannot unset it.

Fail closed: a process that asks for an overlay that does not exist refuses
to start, rather than running on the paper values it was trying to replace.
With BASIS_ENV_OVERLAY unset this is exactly the old single load_dotenv call,
so the paper processes are unchanged.

The live entrypoint (backend/live_entry.py) calls load_env() before importing
anything that captures the environment at import time (database.TRADING_MODE),
and the live executor re-checks the resulting mode at run time anyway.
"""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

REPO_ROOT = Path(__file__).parent.parent
BASE_ENV_FILE = REPO_ROOT / ".env"
ENV_OVERLAY_VAR = "BASIS_ENV_OVERLAY"


def overlay_path() -> Path | None:
    """The overlay file BASIS_ENV_OVERLAY names, resolved against the repo
    root when relative; None when no overlay is requested."""
    name = (os.environ.get(ENV_OVERLAY_VAR) or "").strip()
    if not name:
        return None
    path = Path(name)
    return path if path.is_absolute() else REPO_ROOT / path


def load_env() -> None:
    """Load `.env` (override=True), then the overlay if one is requested.
    Raises RuntimeError when the requested overlay file is missing."""
    load_dotenv(BASE_ENV_FILE, override=True)
    overlay = overlay_path()
    if overlay is None:
        return
    if not overlay.is_file():
        raise RuntimeError(
            f"{ENV_OVERLAY_VAR} names an environment overlay that does not exist ({overlay.name}) — "
            "refusing to start on the base .env alone"
        )
    load_dotenv(overlay, override=True)


def base_env_values() -> dict[str, str | None]:
    """The base `.env` file's own values, ignoring the overlay and the process
    environment — what the PAPER processes see. The live executor reads it to
    prove its Gateway port and start script differ from the paper ones."""
    if not BASE_ENV_FILE.is_file():
        return {}
    return dict(dotenv_values(BASE_ENV_FILE))


# The live pixi tasks' overlay (pixi.toml sets BASIS_ENV_OVERLAY to this name).
LIVE_OVERLAY_FILE = REPO_ROOT / ".env.live"


def live_overlay_values() -> dict[str, str | None]:
    """The live overlay file's own values, read WITHOUT loading them into the
    environment (#1098). The PAPER processes read it for one reason: to
    recognise the persistent live Gateway's processes by their IBC paths, so
    a paper teardown never kills it. Empty when there is no live overlay."""
    if not LIVE_OVERLAY_FILE.is_file():
        return {}
    return dict(dotenv_values(LIVE_OVERLAY_FILE))
