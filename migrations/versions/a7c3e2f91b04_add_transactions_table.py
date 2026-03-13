"""add_transactions_table

Revision ID: a7c3e2f91b04
Revises: 48f9a1171568
Create Date: 2026-03-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e2f91b04'
down_revision: Union[str, Sequence[str], None] = '48f9a1171568'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the transactions audit log table."""
    op.create_table('transactions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ib_order_id', sa.Integer(), nullable=True),
        sa.Column('ib_perm_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.Enum(
            'PLACE_ATTEMPT', 'PLACE_ACCEPTED', 'PLACE_REJECTED',
            'PARTIAL_FILL', 'FILLED', 'CANCEL_ATTEMPT', 'CANCELLED',
            'ERROR_TERMINAL', 'RECONCILED',
            name='transactionaction',
        ), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('side', sa.String(length=4), nullable=False),
        sa.Column('order_type', sa.String(length=10), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('limit_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('account_id', sa.String(length=50), nullable=False),
        sa.Column('ib_status', sa.String(length=50), nullable=True),
        sa.Column('ib_filled_qty', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('ib_avg_fill_price', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('ib_error_code', sa.Integer(), nullable=True),
        sa.Column('ib_error_message', sa.Text(), nullable=True),
        sa.Column('trade_serial', sa.Integer(), nullable=True),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('ib_responded_at', sa.DateTime(), nullable=True),
        sa.Column('is_terminal', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Drop the transactions table."""
    op.drop_table('transactions')
