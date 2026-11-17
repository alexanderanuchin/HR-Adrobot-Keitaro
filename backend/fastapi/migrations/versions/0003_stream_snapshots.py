"""Append-only normalized stream snapshots for three-way conflict detection.

Revision ID: 0003_stream_snapshots
Revises: 0002_campaigns_streams
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_stream_snapshots"
down_revision = "0002_campaigns_streams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kt_stream_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kt_stream_snapshots_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object'",
            name="ck_kt_stream_snapshots_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["kt_streams.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kt_stream_snapshots_stream_captured",
        "kt_stream_snapshots",
        ["stream_id", "captured_at"],
    )

    # Database-level immutability prevents UPDATE/DELETE even through raw SQL.
    op.execute(
        """
        CREATE FUNCTION kt_reject_stream_snapshot_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'kt_stream_snapshots are append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_kt_stream_snapshots_immutable
        BEFORE UPDATE OR DELETE ON kt_stream_snapshots
        FOR EACH ROW EXECUTE FUNCTION kt_reject_stream_snapshot_mutation();
        """
    )


def downgrade() -> None:
    # Downgrade is destructive: do not run against data-bearing environments.
    op.drop_index(
        "ix_kt_stream_snapshots_stream_captured", table_name="kt_stream_snapshots"
    )
    op.drop_table("kt_stream_snapshots")
    op.execute("DROP FUNCTION kt_reject_stream_snapshot_mutation()")
