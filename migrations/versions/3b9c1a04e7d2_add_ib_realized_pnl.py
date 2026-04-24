"""add ib_realized_pnl on trade_groups

IB ships realized P&L on every CommissionReport (the
``CommissionReport.realizedPNL`` field). Capturing that value gives us
authoritative per-position round-trip P&L without having to re-derive it
from cost basis + fill prices on our side. The new column accumulates
those contributions additively from the commission callback, isolated
from ``realized_pnl`` (which bot/close-leg flows write from the engine's
own computation). API serializer prefers ``ib_realized_pnl`` when set
so one-shot user orders show round-trip P&L on close without colliding
with bot-derived values.

Revision ID: 3b9c1a04e7d2
Revises: 2d2c75fd550d
Create Date: 2026-04-23 22:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3b9c1a04e7d2"
down_revision: Union[str, Sequence[str], None] = "2d2c75fd550d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_groups",
        sa.Column("ib_realized_pnl", sa.Numeric(18, 8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_groups", "ib_realized_pnl")
