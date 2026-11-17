"""Create draft metadata with snapshot-consistency and one-open-draft rules.

Revision ID: 0005_stream_drafts
Revises: 0004_stream_offers

This migration only adds kt_stream_drafts and a uniqueness constraint to
kt_stream_snapshots; it does not alter snapshot contents or Django tables.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_stream_drafts"
down_revision = "0004_stream_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Required by the composite FKs below. Snapshot rows remain immutable.
    op.create_unique_constraint(
        "uq_kt_stream_snapshots_id_stream",
        "kt_stream_snapshots",
        ["id", "stream_id"],
    )

    op.create_table(
        "kt_stream_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("latest_remote_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'SYNCED'"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column("conflict_details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('SYNCED', 'DIRTY', 'CONFLICT', 'PUBLISHING', "
            "'VERIFYING', 'UNCERTAIN', 'FAILED', 'PUBLISHED', 'CANCELLED')",
            name="ck_kt_stream_drafts_status",
        ),
        sa.CheckConstraint(
            "revision >= 0", name="ck_kt_stream_drafts_revision_nonnegative"
        ),
        sa.CheckConstraint(
            "created_by_id > 0", name="ck_kt_stream_drafts_creator_positive"
        ),
        sa.CheckConstraint(
            "conflict_details IS NULL OR jsonb_typeof(conflict_details) = 'object'",
            name="ck_kt_stream_drafts_conflict_object",
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["kt_streams.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["base_snapshot_id", "stream_id"],
            ["kt_stream_snapshots.id", "kt_stream_snapshots.stream_id"],
            ondelete="RESTRICT",
            name="fk_kt_stream_drafts_base_snapshot_stream",
        ),
        sa.ForeignKeyConstraint(
            ["latest_remote_snapshot_id", "stream_id"],
            ["kt_stream_snapshots.id", "kt_stream_snapshots.stream_id"],
            ondelete="RESTRICT",
            name="fk_kt_stream_drafts_latest_snapshot_stream",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_kt_stream_drafts_one_unfinished_per_stream",
        "kt_stream_drafts",
        ["stream_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('PUBLISHED', 'CANCELLED')"),
    )
    op.create_index(
        "ix_kt_stream_drafts_stream_created",
        "kt_stream_drafts",
        ["stream_id", "created_at"],
    )


def downgrade() -> None:
    # Destructive for draft data: run only when explicitly approved.
    op.drop_index("ix_kt_stream_drafts_stream_created", table_name="kt_stream_drafts")
    op.drop_index(
        "uq_kt_stream_drafts_one_unfinished_per_stream",
        table_name="kt_stream_drafts",
    )
    op.drop_table("kt_stream_drafts")
    op.drop_constraint(
        "uq_kt_stream_snapshots_id_stream",
        "kt_stream_snapshots",
        type_="unique",
    )
