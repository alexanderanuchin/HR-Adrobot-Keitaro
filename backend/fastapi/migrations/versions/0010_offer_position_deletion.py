"""Hide deleted stream positions while retaining references in draft history.

Revision ID: 0010_offer_position_deletion
Revises: 0009_pending_offer_pins
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_offer_position_deletion"
down_revision = "0009_pending_offer_pins"
branch_labels = None
depends_on = None


def upgrade():
    for table, status_column in (("kt_stream_offers", "local_status"), ("kt_draft_offers", "status")):
        op.add_column(table, sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.create_check_constraint(f"ck_{table}_deleted_status", table, f"NOT is_deleted OR {status_column} = 'REMOVED'")


def downgrade():
    for table in ("kt_draft_offers", "kt_stream_offers"):
        op.drop_constraint(f"ck_{table}_deleted_status", table, type_="check")
        op.drop_column(table, "is_deleted")
