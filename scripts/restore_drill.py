"""CLI entry point for the automated restore drill (#640).

    pixi run restore-drill                                  # sandboxed: oldest backup, scratch copy
    pixi run python scripts/restore_drill.py --backup PATH   # sandboxed: explicit backup
    pixi run python scripts/restore_drill.py --against-production
                                                               # standalone recon-only, live DB, read-only connection

See backend/restore_drill.py for the full design — this file only wires the
CLI to it. Never run this against a real Gateway without a human present for
the first live drill; the tool itself is safe (structurally read-only broker
+ read-only DB connection), but confirming that in person once is cheap
insurance.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The scheduled entry points get .env loaded transitively via backend.main /
# backend.operator; this standalone CLI must do it itself or gateway config
# (IBC_START_SCRIPT et al.) is invisible when launched directly (#643).
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

from backend.restore_drill import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
