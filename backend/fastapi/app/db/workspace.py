"""PostgreSQL sessions and locks for operations with remote side effects."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings


class OperationBusy(Exception):
    """Another worker is already synchronizing or writing this resource."""


@lru_cache
def workspace_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})


def get_workspace_engine(settings: Settings) -> Engine:
    return workspace_engine(settings.database_url)


@contextmanager
def operation_session(engine: Engine, resource: str) -> Iterator[Session]:
    """Keep a PostgreSQL advisory lock across short committed transactions."""
    args = {"resource": f"adrobot:{resource}"}
    with engine.connect() as connection:
        acquired = connection.scalar(text(
            "SELECT pg_try_advisory_lock(hashtextextended(:resource, 0))"
        ), args)
        connection.commit()
        if not acquired:
            raise OperationBusy("Another operation is in progress. Refresh and try again.")
        try:
            with Session(bind=connection, expire_on_commit=False) as session:
                yield session
        finally:
            if not connection.invalidated:
                try:
                    connection.execute(text(
                        "SELECT pg_advisory_unlock(hashtextextended(:resource, 0))"
                    ), args)
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise
