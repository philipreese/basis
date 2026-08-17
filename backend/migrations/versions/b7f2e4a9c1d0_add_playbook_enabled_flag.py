"""Add playbook `enabled` flag and credit-spread seed playbooks

Brings pre-0.7.0 databases up to the schema introduced by commit 266f9ec:
adds the `enabled` column (default true), disables the long straddle/strangle
seed playbooks per the seed intent, and inserts the two credit-spread seed
playbooks if absent. The playbook parameter sets are frozen copies of the
0.7.0 seeds — migrations must not import from live application code.

Revision ID: b7f2e4a9c1d0
Revises: 6640075bcc04
Create Date: 2026-08-17 12:55:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f2e4a9c1d0'
down_revision: Union[str, Sequence[str], None] = '6640075bcc04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CREDIT_SPREAD_SEEDS: list[dict] = [
    {
        "id": "spy_bull_put_spread_v1",
        "version": "1.0",
        "name": "SPY Bull Put Spread — Calm Bull Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 20.0, "max_ivr": 100.0,
            "vix_range": [10.0, 30.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bear_call_spread_v1",
        "version": "1.0",
        "name": "SPY Bear Call Spread — Trending Bear Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BEAR_CALL_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 25.0, "max_ivr": 100.0,
            "vix_range": [15.0, 45.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
]


def upgrade() -> None:
    with op.batch_alter_table('playbooks') as batch:
        batch.add_column(
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('1'))
        )

    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE playbooks SET enabled = 0"
        " WHERE id IN ('spy_long_straddle_v1', 'spy_long_strangle_v1')"
    ))

    for pb in _CREDIT_SPREAD_SEEDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM playbooks WHERE id = :id AND version = :version"),
            {"id": pb["id"], "version": pb["version"]},
        ).fetchone()
        if exists is None:
            conn.execute(
                sa.text(
                    "INSERT INTO playbooks"
                    " (id, version, name, underlying_ticker, strategy_type,"
                    "  execution_mode, enabled, entry_filters, execution_specs, exit_rules)"
                    " VALUES (:id, :version, :name, :underlying_ticker, :strategy_type,"
                    "  :execution_mode, 1, :entry_filters, :execution_specs, :exit_rules)"
                ),
                {
                    "id": pb["id"],
                    "version": pb["version"],
                    "name": pb["name"],
                    "underlying_ticker": pb["underlying_ticker"],
                    "strategy_type": pb["strategy_type"],
                    "execution_mode": pb["execution_mode"],
                    "entry_filters": json.dumps(pb["entry_filters"]),
                    "execution_specs": json.dumps(pb["execution_specs"]),
                    "exit_rules": json.dumps(pb["exit_rules"]),
                },
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM playbooks"
        " WHERE id IN ('spy_bull_put_spread_v1', 'spy_bear_call_spread_v1')"
    ))
    with op.batch_alter_table('playbooks') as batch:
        batch.drop_column('enabled')
