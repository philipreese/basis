"""Add orders.encumbered_risk for pending-order capital encumbrance

Capital reserved by a staged/submitted OPEN order must survive a crash so
the deployed-capital gate keeps counting it on the next run (design §4.3,
#67). Released by moving the order to a terminal status, not by deletion.

Revision ID: d1f5c8a3b9e2
Revises: c9a4b7e2d5f8
Create Date: 2026-08-18 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f5c8a3b9e2"
down_revision: str | Sequence[str] | None = "c9a4b7e2d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("encumbered_risk", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("encumbered_risk")
