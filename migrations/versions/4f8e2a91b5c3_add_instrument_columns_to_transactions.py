"""add trading_class/multiplier/con_id to transactions

Epic 1 widened the ``Transaction`` model with three instrument fields so
a cold-restart reconciler can rebuild the Instrument from archival rows
without a live IB call (data/models.py:262-264). The schema migration
was never authored, so the model and table drifted: any INSERT that
referenced these columns failed with ``no column named trading_class``,
which then poisoned the SQLAlchemy session for downstream writes
(commission apply, heartbeat upsert).

Columns are nullable for back-compat with pre-Epic-1 rows.

Revision ID: 4f8e2a91b5c3
Revises: 3b9c1a04e7d2
Create Date: 2026-04-27 09:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4f8e2a91b5c3"
down_revision: Union[str, Sequence[str], None] = "3b9c1a04e7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("trading_class", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("multiplier", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("con_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "con_id")
    op.drop_column("transactions", "multiplier")
    op.drop_column("transactions", "trading_class")
