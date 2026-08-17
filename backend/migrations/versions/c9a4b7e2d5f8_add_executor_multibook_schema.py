"""Add the Executor (Paper) multi-book schema

Creates the book/order/fill chain, reconciliation and audit tables,
trading control, regime readings, and index history per
spec/data-models.md → "Executor (Paper) schema additions" (#61).
Existing positions are backfilled into the 'B00' legacy book; a GLOBAL
trading-control row is seeded ACTIVE.

Revision ID: c9a4b7e2d5f8
Revises: b7f2e4a9c1d0
Create Date: 2026-08-18 09:30:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9a4b7e2d5f8"
down_revision: str | Sequence[str] | None = "b7f2e4a9c1d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("starting_capital", sa.Float(), nullable=False),
        sa.Column("cash_balance", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=True),
        sa.Column("order_ref", sa.String(), nullable=False),
        sa.Column("ib_order_id", sa.Integer(), nullable=True),
        sa.Column("ib_perm_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("combo_legs", sa.JSON(), nullable=False),
        sa.Column("order_type", sa.String(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=False),
        sa.Column("decision_midpoint", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.String(), nullable=True),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_ref"),
    )
    op.create_index("ix_orders_book_id", "orders", ["book_id"])
    op.create_table(
        "fills",
        sa.Column("exec_id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("con_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("fill_time", sa.String(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.PrimaryKeyConstraint("exec_id"),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_at", sa.String(), nullable=False),
        sa.Column("broker_snapshot", sa.JSON(), nullable=False),
        sa.Column("books_expected", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("drift_details", sa.JSON(), nullable=True),
        sa.Column("resolved_at", sa.String(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gate_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("run_at", sa.String(), nullable=False),
        sa.Column("gate", sa.String(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_at", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "trading_control",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("changed_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_table(
        "regime_readings",
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("engine_variant", sa.String(), nullable=False),
        sa.Column("regime", sa.String(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("date", "book_id", "engine_variant"),
    )
    op.create_table(
        "index_history",
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("date", "symbol"),
    )

    now = datetime.now(UTC).isoformat()
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO books (id, name, config, config_version, config_hash,"
            " starting_capital, cash_balance, status, created_at)"
            " VALUES ('B00', 'Legacy — pre-executor manual positions', '{}', 1, '',"
            " 10000.0, 10000.0, 'LEGACY', :now)"
        ),
        {"now": now},
    )
    conn.execute(
        sa.text(
            "INSERT INTO trading_control (scope, state, reason, actor, changed_at)"
            " VALUES ('GLOBAL', 'ACTIVE', 'Initial state', 'migration', :now)"
        ),
        {"now": now},
    )

    with op.batch_alter_table("positions") as batch:
        batch.add_column(sa.Column("book_id", sa.String(), nullable=False, server_default="B00"))
        batch.create_foreign_key("fk_positions_book_id_books", "books", ["book_id"], ["id"])
    op.create_index("ix_positions_book_id", "positions", ["book_id"])


def downgrade() -> None:
    op.drop_index("ix_positions_book_id", "positions")
    with op.batch_alter_table("positions") as batch:
        batch.drop_constraint("fk_positions_book_id_books", type_="foreignkey")
        batch.drop_column("book_id")
    op.drop_table("index_history")
    op.drop_table("regime_readings")
    op.drop_table("trading_control")
    op.drop_table("audit_events")
    op.drop_table("gate_events")
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_fills_order_id", "fills")
    op.drop_table("fills")
    op.drop_index("ix_orders_book_id", "orders")
    op.drop_table("orders")
    op.drop_table("books")
