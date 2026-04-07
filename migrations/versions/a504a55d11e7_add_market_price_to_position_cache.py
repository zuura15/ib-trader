"""add market_price to position_cache

Revision ID: a504a55d11e7
Revises: 28646b547eeb
Create Date: 2026-03-23 08:10:05.207022

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a504a55d11e7'
down_revision: Union[str, Sequence[str], None] = '28646b547eeb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # position_cache is not created by any prior migration; on a fresh DB
    # this ALTER would fail. The next migration drops the table defensively,
    # so skip when it doesn't exist.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'position_cache' in inspector.get_table_names():
        op.add_column('position_cache', sa.Column('market_price', sa.Numeric(precision=18, scale=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'position_cache' in inspector.get_table_names():
        op.drop_column('position_cache', 'market_price')
