"""Track the last edit of each stream for workspace navigation.

Revision ID: 0011_stream_edit_history
Revises: 0010_offer_position_deletion
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_stream_edit_history"
down_revision = "0010_offer_position_deletion"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("kt_streams", sa.Column("last_edited_at", sa.DateTime(timezone=True), nullable=True))
    # Older records distinguish saved edits from untouched drafts, but not from auto-cancelled drafts.
    op.execute("""
        UPDATE kt_streams AS stream
        SET last_edited_at = edits.last_edited_at
        FROM (
            SELECT stream_id, max(updated_at) AS last_edited_at
            FROM kt_stream_drafts
            WHERE status NOT IN ('SYNCED', 'CANCELLED')
            GROUP BY stream_id
        ) AS edits
        WHERE stream.id = edits.stream_id
    """)
    op.create_index("ix_kt_streams_last_edited", "kt_streams", ["last_edited_at", "id"],
                    postgresql_where=sa.text("last_edited_at IS NOT NULL"))


def downgrade():
    op.drop_index("ix_kt_streams_last_edited", table_name="kt_streams")
    op.drop_column("kt_streams", "last_edited_at")
