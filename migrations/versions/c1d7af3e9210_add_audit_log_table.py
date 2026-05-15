"""add audit_log table

Revision ID: c1d7af3e9210
Revises: 4f8e2a91b5c3
Create Date: 2026-05-14 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d7af3e9210"
down_revision: Union[str, Sequence[str], None] = "4f8e2a91b5c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("event_ts_utc", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("pivot_status", sa.String(length=20), nullable=True),
        sa.Column("line_status", sa.String(length=20), nullable=True),
        sa.Column("decision", sa.String(length=60), nullable=False),
        sa.Column("bar_close", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("pnl_net", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_log_bot_id", "audit_log", ["bot_id"])
    op.create_index("ix_audit_log_symbol", "audit_log", ["symbol"])
    op.create_index("ix_audit_log_event_ts_utc", "audit_log", ["event_ts_utc"])
    op.create_index("ix_audit_bot_ts", "audit_log",
                    ["bot_id", "event_ts_utc"])
    op.create_index("ix_audit_ts", "audit_log", ["event_ts_utc"])
    op.create_index("ix_audit_bot_type_ts", "audit_log",
                    ["bot_id", "event_type", "event_ts_utc"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_audit_bot_type_ts", table_name="audit_log")
    op.drop_index("ix_audit_ts", table_name="audit_log")
    op.drop_index("ix_audit_bot_ts", table_name="audit_log")
    op.drop_index("ix_audit_log_event_ts_utc", table_name="audit_log")
    op.drop_index("ix_audit_log_symbol", table_name="audit_log")
    op.drop_index("ix_audit_log_bot_id", table_name="audit_log")
    op.drop_table("audit_log")
