"""Persist campaign creation intent and stream offer ordering.

Revision ID: 0007_workspace_operations
Revises: 0006_draft_offers
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_workspace_operations"
down_revision = "0006_draft_offers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "kt_campaign_operations",
        sa.Column("alias", sa.String(64), primary_key=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("campaign_id", sa.BigInteger()),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'UNCERTAIN', 'COMPLETED')",
            name="ck_kt_campaign_operations_status",
        ),
    )
    op.add_column("kt_stream_offers", sa.Column(
        "position_index", sa.Integer(), nullable=False, server_default=sa.text("0"),
    ))


def downgrade():
    op.drop_column("kt_stream_offers", "position_index")
    op.drop_table("kt_campaign_operations")
