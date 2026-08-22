"""CLI entry point for the empirical-null bootstrap drill (#657).

    pixi run empirical-null-drill                                          # against production, defaults
    pixi run python scripts/empirical_null_drill.py --database PATH        # explicit DB
    pixi run python scripts/empirical_null_drill.py --iterations 20000 --seed 1

See backend/empirical_null_drill.py for the full design (in particular the
module docstring's "what null this constructs" section — read it before
trusting a number) — this file only wires the CLI to it. Ledger-only: no
Gateway, no broker connection; the DB is opened through the same read-only
mode=ro connection restore_drill.py uses, so this is safe to run any time.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.empirical_null_drill import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
