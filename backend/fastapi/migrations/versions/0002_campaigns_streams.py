"""Create kt_campaigns and kt_streams (A-04, phase 1).

Revision ID: 0002_campaigns_streams
Revises: 0001_baseline
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_campaigns_streams"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kt_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("keitaro_campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("domain_id", sa.BigInteger(), nullable=True),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("traffic_source_id", sa.BigInteger(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "keitaro_campaign_id > 0",
            name="ck_kt_campaigns_keitaro_campaign_id_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "keitaro_campaign_id", name="uq_kt_campaigns_keitaro_campaign_id"
        ),
    )

    op.create_table(
        "kt_streams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("keitaro_stream_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("stream_type", sa.String(length=32), nullable=False),
        sa.Column("schema_type", sa.String(length=32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "keitaro_stream_id > 0",
            name="ck_kt_streams_keitaro_stream_id_positive",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["kt_campaigns.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "keitaro_stream_id", name="uq_kt_streams_keitaro_stream_id"
        ),
    )

    op.create_index(
        "ix_kt_streams_campaign_id", "kt_streams", ["campaign_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_kt_streams_campaign_id", table_name="kt_streams")
    op.drop_table("kt_streams")
    op.drop_table("kt_campaigns")
