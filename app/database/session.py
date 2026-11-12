from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine = None
_factory: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _factory
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request."""
    get_engine()
    assert _factory is not None
    with _factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


from contextlib import contextmanager  # noqa: E402


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction scope for workers/tasks (the API uses the get_session dependency)."""
    get_engine()
    assert _factory is not None
    with _factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
