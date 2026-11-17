"""Persist offer positions and locally archived stream/offer associations.

Revision ID: 0004_stream_offers
Revises: 0003_stream_snapshots
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_stream_offers"
down_revision = "0003_stream_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kt_stream_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("keitaro_offer_id", sa.BigInteger(), nullable=False),
        sa.Column("keitaro_relation_id", sa.BigInteger(), nullable=True),
        sa.Column("remote_share", sa.Numeric(), nullable=True),
        sa.Column("remote_state", sa.String(length=32), nullable=True),
        sa.Column(
            "local_status",
            sa.String(length=16),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("pinned_percent", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "keitaro_offer_id > 0",
            name="ck_kt_stream_offers_offer_id_positive",
        ),
        sa.CheckConstraint(
            "keitaro_relation_id IS NULL OR keitaro_relation_id > 0",
            name="ck_kt_stream_offers_relation_id_positive",
        ),
        sa.CheckConstraint(
            "remote_share IS NULL OR remote_share >= 0",
            name="ck_kt_stream_offers_remote_share_nonnegative",
        ),
        sa.CheckConstraint(
            "local_status IN ('ACTIVE', 'REMOVED')",
            name="ck_kt_stream_offers_local_status",
        ),
        sa.CheckConstraint(
            "pinned_percent IS NULL OR pinned_percent BETWEEN 0 AND 100",
            name="ck_kt_stream_offers_pinned_percent_range",
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["kt_streams.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kt_stream_offers_stream_status",
        "kt_stream_offers",
        ["stream_id", "local_status"],
    )
    op.create_index(
        "ix_kt_stream_offers_stream_offer",
        "kt_stream_offers",
        ["stream_id", "keitaro_offer_id"],
    )


def downgrade() -> None:
    # Destructive: remove only after ensuring no offer-position data is needed.
    op.drop_index("ix_kt_stream_offers_stream_offer", table_name="kt_stream_offers")
    op.drop_index("ix_kt_stream_offers_stream_status", table_name="kt_stream_offers")
    op.drop_table("kt_stream_offers")
