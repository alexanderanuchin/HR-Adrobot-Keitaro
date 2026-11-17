from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """SQLAlchemy owns only kt_* tables; model declarations come next commit."""
