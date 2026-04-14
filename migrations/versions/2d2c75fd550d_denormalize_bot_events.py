"""denormalize_bot_events

Adds bot_name, strategy, config_version columns on bot_events so events
remain readable after a bot's YAML is edited or removed. Adds an index on
bot_name to support UI queries by name. Drops the FK on bot_id so that
deleting a YAML-defined bot does not cascade into the event audit trail.

Revision ID: 2d2c75fd550d
Revises: 082a73d8f5c5
Create Date: 2026-04-14 11:28:38.617496

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2d2c75fd550d'
down_revision: Union[str, Sequence[str], None] = '082a73d8f5c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bot_events") as batch:
        batch.add_column(sa.Column("bot_name", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("strategy", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("config_version", sa.String(length=32), nullable=True))
        batch.create_index("ix_bot_events_bot_name", ["bot_name"])
        # Drop the FK on bot_id — bot identity lives in YAML now, and
        # deleting a YAML must not cascade into the audit trail.
        for fk in list(batch.impl.table.foreign_key_constraints):
            batch.drop_constraint(fk.name, type_="foreignkey")


def downgrade() -> None:
    with op.batch_alter_table("bot_events") as batch:
        batch.drop_index("ix_bot_events_bot_name")
        batch.drop_column("config_version")
        batch.drop_column("strategy")
        batch.drop_column("bot_name")
        batch.create_foreign_key(
            "fk_bot_events_bot_id_bots",
            "bots",
            ["bot_id"],
            ["id"],
        )
