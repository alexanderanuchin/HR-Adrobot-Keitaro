from sqlalchemy import create_engine, text
from app.core.config import get_settings

def database_ready() -> bool:
    """Short-lived connection for readiness only; business sessions are not implemented yet."""
    engine = create_engine(get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            return bool(connection.execute(text("SELECT 1")).scalar_one() == 1)
    finally:
        engine.dispose()
