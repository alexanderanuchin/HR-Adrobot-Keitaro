"""Record campaign creation attempts before the first Keitaro write."""

from sqlalchemy import BigInteger, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CampaignOperation(Base):
    __tablename__ = "kt_campaign_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'UNCERTAIN', 'COMPLETED')",
            name="ck_kt_campaign_operations_status",
        ),
    )

    alias: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
