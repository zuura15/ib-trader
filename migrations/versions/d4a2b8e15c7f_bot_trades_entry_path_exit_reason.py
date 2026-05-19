"""bot_trades: add entry_path + exit_reason for analytics

Revision ID: d4a2b8e15c7f
Revises: c1d7af3e9210
Create Date: 2026-05-19 08:00:00.000000

Adds two nullable columns to bot_trades so SQL analytics can
distinguish organic entries (touch / accel) from operator-forced
ones (force), and group trades by exit trigger (trail_stop /
counter_line / line_breach / force_quit / ...).

Both columns are populated from the existing TRADE_CLOSED audit
payload — no behavior change, just persistence to the canonical
closed-trades table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a2b8e15c7f"
down_revision: Union[str, Sequence[str], None] = "c1d7af3e9210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bot_trades" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("bot_trades")}
    if "entry_path" not in cols:
        op.add_column(
            "bot_trades",
            sa.Column("entry_path", sa.String(length=32), nullable=True),
        )
    if "exit_reason" not in cols:
        op.add_column(
            "bot_trades",
            sa.Column("exit_reason", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bot_trades" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("bot_trades")}
    if "exit_reason" in cols:
        op.drop_column("bot_trades", "exit_reason")
    if "entry_path" in cols:
        op.drop_column("bot_trades", "entry_path")
