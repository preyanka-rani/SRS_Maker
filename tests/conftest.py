"""Shared fixtures. Integration tests run against an embedded PostgreSQL (pgserver) that is
migrated with the real Alembic migrations, so triggers and composite FKs are exercised."""
import os
import pathlib
import tempfile
import uuid

# Must be set before any app module reads settings.
os.environ.setdefault("LOCAL_SCHEDULER", "false")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-0123456789abcdef")
os.environ.setdefault("STORAGE_ROOT", tempfile.mkdtemp(prefix="srs_store_"))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.security import hash_password


@pytest.fixture(scope="session")
def pg_url():
    import pgserver

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="srs_pg_"))
    server = pgserver.get_server(tmp / "pg")
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    # throwaway test server: durability is irrelevant, speed matters
    for setting in ("fsync = off", "synchronous_commit = off", "full_page_writes = off"):
        server.psql(f"ALTER SYSTEM SET {setting};")
    server.psql("SELECT pg_reload_conf();")
    os.environ["DATABASE_URL"] = url
    _STATE["url"] = url
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield url
    server.cleanup()


@pytest.fixture(scope="session")
def engine(pg_url):
    eng = create_engine(pg_url)
    yield eng
    eng.dispose()


_STATE: dict = {"url": None}


@pytest.fixture(autouse=True)
def _clean_db():
    """Truncate all tables after each test that used the database (TRUNCATE skips row triggers)."""
    yield
    url = _STATE["url"]
    if not url:
        return
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            tables = conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename <> 'alembic_version'")).scalars().all()
            if tables:
                conn.execute(text("TRUNCATE " + ",".join(f'"{t}"' for t in tables) + " CASCADE"))
    finally:
        eng.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture
def api(pg_url):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.database import session as session_mod

    get_settings.cache_clear()
    session_mod._engine = None
    session_mod._factory = None
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


def make_user(db: Session, role: str = "admin", email: str | None = None,
              password: str = "pw-12345") -> tuple[uuid.UUID, str, str]:
    from app.database.models import AppUser, UserRole

    email = email or f"{role}-{uuid.uuid4().hex[:6]}@example.com"
    user = AppUser(email=email, password_hash=hash_password(password), role=UserRole(role))
    db.add(user)
    db.commit()
    return user.id, email, password


def login(api, email: str, password: str) -> dict:
    r = api.post("/api/v1/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


@pytest.fixture
def app_db(pg_url):
    """Point app-level engine/settings at the test database (for workers and the runner)."""
    from app.core.config import get_settings
    from app.database import session as session_mod

    get_settings.cache_clear()
    session_mod._engine = None
    session_mod._factory = None
    yield
    session_mod._engine = None
    session_mod._factory = None
