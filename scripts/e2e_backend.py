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


async def _seed_e2e_fixtures() -> None:
    """Extra fixtures beyond init_db's own seeds, for e2e specs that need
    state no legitimate API call can create (#480): a DRIFT reconciliation
    run, a PARTIAL order, and a live SUBMITTED close order (for the
    acknowledge_cancelled refusal, #407) — there is no POST /api/orders or
    POST /api/reconciliation endpoint, by design (orders are executor-only;
    reconciliation runs are written by the nightly sync). Positions
    themselves ARE seeded through the real API by the specs that need them
    (POST /api/positions, matching 50-close-position.spec.ts), keeping this
    function to only the state that's genuinely unreachable any other way.
    init_db() runs first so the schema (and its own book/playbook seeds)
    exist before these inserts — it's additive/idempotent, so main.py's own
    startup lifespan calling it again afterward is a no-op."""
    from backend.database import async_session_maker, init_db
    from backend.models import OrderModel, ReconciliationRunModel

    await init_db()
    async with async_session_maker() as session:
        session.add(
            ReconciliationRunModel(
                run_at="2026-08-20T22:00:00+00:00",
                broker_snapshot={},
                books_expected={},
                result="DRIFT",
                drift_details=[
                    {
                        "kind": "ORPHAN",
                        "key": "AAPL261016C00230000",
                        "sec_type": "OPT",
                        "broker_qty": 1.0,
                        "expected_qty": 0.0,
                        "unexpected_instrument": False,
                    },
                ],
            )
        )
        # A standalone PARTIAL entry order — no position, per
        # resolve_partial_order's own docstring ("a partial ENTRY has no
        # position, so no external close applies").
        session.add(
            OrderModel(
                id="e2e_partial_1",
                book_id="B01",
                position_id=None,
                order_ref="basis:B01:e2e_partial_1:open",
                action="OPEN",
                combo_legs={"legs": [], "quantity": 1},
                order_type="LIMIT",
                limit_price=1.0,
                decision_midpoint=1.0,
                status="PARTIAL",
                encumbered_risk=100.0,
            )
        )
        # A live close order referencing a FIXED position id the spec seeds
        # itself via POST /api/positions — SQLite FK enforcement is off in
        # this codebase (backend/database.py never sets PRAGMA foreign_keys),
        # so the row is valid to insert before that position exists.
        session.add(
            OrderModel(
                id="e2e_live_close_1",
                book_id="B01",
                position_id="e2e-pos-ack-cancel",
                order_ref="basis:B01:e2e_live_close_1:close",
                action="CLOSE",
                combo_legs={"legs": [], "quantity": 1},
                order_type="LIMIT",
                limit_price=1.0,
                decision_midpoint=1.0,
                status="SUBMITTED",
                submitted_at="2026-08-21T20:00:00+00:00",
            )
        )
        await session.commit()


def main() -> None:
    _DB.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB.as_posix()}"
    # Keep the sentinel away from any real HALT file the operator might use.
    os.environ.setdefault("HALT_FILE", str(_ROOT / "e2e-smoke.HALT"))
    # Same isolation for the heartbeat: once the real executor has run, the
    # repo-root heartbeat exists and "never run" assertions would lie.
    hb = _ROOT / "e2e-smoke.heartbeat.json"
    hb.unlink(missing_ok=True)
    os.environ["EXECUTOR_HEARTBEAT_FILE"] = str(hb)

    import asyncio

    asyncio.run(_seed_e2e_fixtures())

    import uvicorn

    uvicorn.run("backend.main:app", port=8630, log_level="warning")


if __name__ == "__main__":
    main()
