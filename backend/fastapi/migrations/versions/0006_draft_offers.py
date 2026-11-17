"""Isolated draft offer positions with composite ownership checks.

Revision ID: 0006_draft_offers
Revises: 0005_stream_drafts

No existing data is updated. These additional unique constraints allow
PostgreSQL to enforce the draft/stream and source-offer identities.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_draft_offers"
down_revision = "0005_stream_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_kt_stream_drafts_id_stream",
        "kt_stream_drafts",
        ["id", "stream_id"],
    )
    op.create_unique_constraint(
        "uq_kt_stream_offers_id_stream_offer",
        "kt_stream_offers",
        ["id", "stream_id", "keitaro_offer_id"],
    )

    op.create_table(
        "kt_draft_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("keitaro_offer_id", sa.BigInteger(), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("pinned_percent", sa.Integer(), nullable=True),
        sa.Column("calculated_percent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "position_index >= 0", name="ck_kt_draft_offers_position_nonnegative"
        ),
        sa.CheckConstraint(
            "keitaro_offer_id > 0", name="ck_kt_draft_offers_offer_id_positive"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REMOVED')", name="ck_kt_draft_offers_status"
        ),
        sa.CheckConstraint(
            "pinned_percent IS NULL OR pinned_percent BETWEEN 0 AND 100",
            name="ck_kt_draft_offers_pinned_range",
        ),
        sa.CheckConstraint(
            "calculated_percent BETWEEN 0 AND 100",
            name="ck_kt_draft_offers_calculated_range",
        ),
        sa.CheckConstraint(
            "status <> 'REMOVED' OR calculated_percent = 0",
            name="ck_kt_draft_offers_removed_zero",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "stream_id"],
            ["kt_stream_drafts.id", "kt_stream_drafts.stream_id"],
            ondelete="RESTRICT", name="fk_kt_draft_offers_draft_stream",
        ),
        sa.ForeignKeyConstraint(
            ["source_position_id", "stream_id", "keitaro_offer_id"],
            ["kt_stream_offers.id", "kt_stream_offers.stream_id", "kt_stream_offers.keitaro_offer_id"],
            ondelete="RESTRICT", name="fk_kt_draft_offers_source_position_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "position_index", name="uq_kt_draft_offers_position"),
        sa.UniqueConstraint("draft_id", "source_position_id", name="uq_kt_draft_offers_source"),
    )
    op.create_index(
        "ix_kt_draft_offers_draft_status", "kt_draft_offers", ["draft_id", "status"]
    )


def downgrade() -> None:
    # Destructive for draft data: never downgrade a data-bearing DB casually.
    op.drop_index("ix_kt_draft_offers_draft_status", table_name="kt_draft_offers")
    op.drop_table("kt_draft_offers")
    op.drop_constraint(
        "uq_kt_stream_offers_id_stream_offer", "kt_stream_offers", type_="unique"
    )
    op.drop_constraint(
        "uq_kt_stream_drafts_id_stream", "kt_stream_drafts", type_="unique"
    )
