"""Append hash-chained audit entries. Serialized per transaction via an advisory lock."""
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database.models import AuditLog
from app.domain.audit_chain import GENESIS_HASH, compute_entry_hash

_AUDIT_LOCK_KEY = 7_340_001  # arbitrary constant; one global chain


def record(
    session: Session,
    *,
    actor_type: str,
    actor_id: str | None,
    entity_type: str,
    action: str,
    entity_id: str | None = None,
    project_id: Any = None,
    details: dict | None = None,
    correlation_id: str | None = None,
) -> AuditLog:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})
    prev = session.execute(
        select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = prev or GENESIS_HASH
    body = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "project_id": str(project_id) if project_id else None,
        "details": details,
        "correlation_id": correlation_id,
    }
    entry = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        details=details,
        correlation_id=correlation_id,
        prev_hash=prev_hash,
        hash=compute_entry_hash(prev_hash, body),
    )
    session.add(entry)
    session.flush()
    return entry
