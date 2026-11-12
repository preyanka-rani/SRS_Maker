"""Local start-up steps used by `python main.py`: database, migrations, first admin user.

Nothing here runs when the app is imported (tests, uvicorn workers); it is only called from
`app.main.run()`.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare_database() -> None:
    """Start an embedded PostgreSQL (data in ./.pgdata) unless USE_EXTERNAL_DB=true."""
    if os.environ.get("USE_EXTERNAL_DB", "").lower() == "true":
        return
    import pgserver

    server = pgserver.get_server(ROOT / ".pgdata")
    os.environ["DATABASE_URL"] = server.get_uri().replace("postgresql://", "postgresql+psycopg://")


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


def prepare_workflow_tables(settings) -> None:
    from app.workflows.runner import prepare_checkpoints

    prepare_checkpoints(settings)


def ensure_admin() -> None:
    """Create the first staff user from ADMIN_EMAIL / ADMIN_PASSWORD (only if it is missing)."""
    email, password = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
    if not (email and password):
        return
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.database.models import AppUser, UserRole
    from app.database.session import session_scope

    with session_scope() as session:
        if session.execute(select(AppUser.id).where(AppUser.email == email.lower())).first() is None:
            session.add(AppUser(email=email.lower(), password_hash=hash_password(password),
                                role=UserRole.admin))
            print(f"created admin user {email}")


def report(settings) -> None:
    print("mailbox:", settings.imap_username or "(not configured)")
    if not settings.mail_configured:
        print("WARNING: IMAP/SMTP settings are incomplete")
    if settings.gemini_api_key is None or not settings.gemini_model_reasoning:
        print("WARNING: GEMINI_API_KEY / GEMINI_MODEL_REASONING are missing")
