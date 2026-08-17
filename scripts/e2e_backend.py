"""Fresh-database backend for the Playwright smoke pack (#83).

Started by Playwright's webServer (frontend/playwright.config.ts). Deletes
the e2e database so every run boots against a clean, freshly seeded stack —
the smoke pack must prove the app boots from nothing, not from leftovers.

Note: backend/main.py loads .env with override=True, so a DATABASE_URL set
there would defeat this isolation. Don't put DATABASE_URL in .env.
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "e2e-smoke.db"

sys.path.insert(0, str(_ROOT))  # make `backend` importable when run as a script


def main() -> None:
    _DB.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB.as_posix()}"
    # Keep the sentinel away from any real HALT file the operator might use.
    os.environ.setdefault("HALT_FILE", str(_ROOT / "e2e-smoke.HALT"))

    import uvicorn

    uvicorn.run("backend.main:app", port=8630, log_level="warning")


if __name__ == "__main__":
    main()
