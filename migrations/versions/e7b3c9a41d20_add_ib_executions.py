"""ib_executions: durable archive of every IB execution (GH #101)

Revision ID: e7b3c9a41d20
Revises: d4a2b8e15c7f
Create Date: 2026-11-08 18:00:00.000000

IB's reqExecutions only returns ~24 h of fills, so futures positions
older than that had no lot history for the LIFO P&L view. This table
archives every execution the engine sees (account-wide, including TWS
and mobile fills) so older lots can be rebuilt and verified against IB.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b3c9a41d20"
down_revision: Union[str, Sequence[str], None] = "d4a2b8e15c7f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if "ib_executions" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "ib_executions",
        sa.Column("exec_id", sa.String(length=64), primary_key=True),
        sa.Column("perm_id", sa.Integer(), nullable=True),
        sa.Column("local_symbol", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("sec_type", sa.String(length=8), nullable=True),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("shares", sa.Numeric(18, 4), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("commission", sa.Numeric(18, 8), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(18, 8), nullable=True),
        sa.Column("multiplier", sa.String(length=16), nullable=True),
        sa.Column("exec_time", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ib_exec_symbol_time", "ib_executions", ["local_symbol", "exec_time"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if "ib_executions" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_ib_exec_symbol_time", table_name="ib_executions")
    op.drop_table("ib_executions")
