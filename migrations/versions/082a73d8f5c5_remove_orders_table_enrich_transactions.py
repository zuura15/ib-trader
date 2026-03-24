"""remove orders table, enrich transactions

Revision ID: 082a73d8f5c5
Revises: a504a55d11e7
Create Date: 2026-03-24 12:26:20.498738

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '082a73d8f5c5'
down_revision: Union[str, Sequence[str], None] = 'a504a55d11e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop unused tables (if they exist — may have been dropped by init_db)
    from sqlalchemy import inspect
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()
    if 'position_cache' in existing:
        op.drop_table('position_cache')
    if 'metrics' in existing:
        op.drop_table('metrics')

    # Enrich transactions with order-table replacement columns
    op.add_column('transactions', sa.Column('trade_id', sa.String(length=36), nullable=True))
    op.add_column('transactions', sa.Column('leg_type', sa.Enum('ENTRY', 'PROFIT_TAKER', 'STOP_LOSS', 'CLOSE', name='legtype'), nullable=True))
    op.add_column('transactions', sa.Column('commission', sa.Numeric(precision=18, scale=8), nullable=True))
    op.add_column('transactions', sa.Column('price_placed', sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column('transactions', sa.Column('correlation_id', sa.String(length=36), nullable=True))
    op.add_column('transactions', sa.Column('security_type', sa.String(length=10), nullable=True))
    op.add_column('transactions', sa.Column('expiry', sa.String(length=10), nullable=True))
    op.add_column('transactions', sa.Column('strike', sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column('transactions', sa.Column('right', sa.String(length=4), nullable=True))
    op.add_column('transactions', sa.Column('raw_response', sa.Text(), nullable=True))

    # Add trade_config to trade_groups
    op.add_column('trade_groups', sa.Column('trade_config', sa.Text(), nullable=True))

    # Migrate reprice_events: add correlation_id, make order_id nullable, drop FK.
    # SQLite requires batch mode for ALTER COLUMN and DROP CONSTRAINT.
    # The FK is unnamed, so we use naming_convention to let batch mode find it.
    naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    with op.batch_alter_table('reprice_events', naming_convention=naming) as batch_op:
        batch_op.add_column(sa.Column('correlation_id', sa.String(length=36), nullable=True))
        batch_op.alter_column('order_id', existing_type=sa.String(length=36), nullable=True)
        batch_op.drop_constraint('fk_reprice_events_order_id_orders', type_='foreignkey')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transactions', 'raw_response')
    op.drop_column('transactions', 'right')
    op.drop_column('transactions', 'strike')
    op.drop_column('transactions', 'expiry')
    op.drop_column('transactions', 'security_type')
    op.drop_column('transactions', 'correlation_id')
    op.drop_column('transactions', 'price_placed')
    op.drop_column('transactions', 'commission')
    op.drop_column('transactions', 'leg_type')
    op.drop_column('transactions', 'trade_id')
    op.drop_column('trade_groups', 'trade_config')

    with op.batch_alter_table('reprice_events') as batch_op:
        batch_op.alter_column('order_id', existing_type=sa.String(length=36), nullable=False)
        batch_op.drop_column('correlation_id')
