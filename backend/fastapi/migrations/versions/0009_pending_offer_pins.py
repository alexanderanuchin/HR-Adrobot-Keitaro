"""Save pending pins separately from currently active pins.

Revision ID: 0009_pending_offer_pins
Revises: 0008_offer_pin_mode
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_pending_offer_pins"
down_revision = "0008_offer_pin_mode"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("kt_stream_offers", "kt_draft_offers"):
        op.add_column(table, sa.Column("pending_pinned_percent", sa.Integer(), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_pending_pin_range", table,
            "pending_pinned_percent IS NULL OR pending_pinned_percent BETWEEN 0 AND 100",
        )


def downgrade():
    for table in ("kt_draft_offers", "kt_stream_offers"):
        op.drop_constraint(f"ck_{table}_pending_pin_range", table, type_="check")
        op.drop_column(table, "pending_pinned_percent")
