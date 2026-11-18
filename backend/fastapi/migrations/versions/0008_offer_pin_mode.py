"""Persist whether a pinned share also applies to a sole active offer.

Revision ID: 0008_offer_pin_mode
Revises: 0007_workspace_operations
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_offer_pin_mode"
down_revision = "0007_workspace_operations"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("kt_stream_offers", "kt_draft_offers"):
        op.add_column(table, sa.Column("pin_immediately", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    for table in ("kt_draft_offers", "kt_stream_offers"):
        op.drop_column(table, "pin_immediately")
