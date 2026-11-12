"""Idempotent issue creation (fingerprint upsert) and manual-review escalation."""
import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    IssueStatus,
    IssueType,
    ManualReview,
    RequirementIssue,
    RequirementIssueLink,
)


def fingerprint(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def raise_issue(session: Session, project_id: uuid.UUID, issue_type: IssueType, title: str,
                *, blocking: bool = False, description: str | None = None,
                why_it_matters: str | None = None, key: str | None = None,
                related_input_source_id: uuid.UUID | None = None,
                requirement_ids: list[uuid.UUID] | None = None) -> RequirementIssue:
    """Create the issue unless an identical (project, fingerprint) one already exists."""
    fp = fingerprint(project_id, issue_type.value, key or title)
    existing = session.execute(
        select(RequirementIssue).where(
            RequirementIssue.project_id == project_id, RequirementIssue.fingerprint == fp)
    ).scalar_one_or_none()
    if existing:
        return existing
    issue = RequirementIssue(
        project_id=project_id, issue_type=issue_type, title=title, blocking=blocking,
        description=description, why_it_matters=why_it_matters, fingerprint=fp,
        related_input_source_id=related_input_source_id, status=IssueStatus.open,
    )
    session.add(issue)
    session.flush()
    for rid in requirement_ids or []:
        session.add(RequirementIssueLink(project_id=project_id, issue_id=issue.id,
                                         requirement_id=rid))
    session.flush()
    return issue


def open_manual_review(session: Session, project_id: uuid.UUID, reason_code: str, *,
                       details: str | None = None, entity_type: str | None = None,
                       entity_id: str | None = None,
                       workflow_run_id: uuid.UUID | None = None) -> ManualReview:
    fp = fingerprint(project_id, reason_code, entity_type, entity_id)
    existing = session.execute(
        select(ManualReview).where(ManualReview.fingerprint == fp,
                                   ManualReview.status == "open")
    ).scalar_one_or_none()
    if existing:
        return existing
    # a previously resolved review with the same fingerprint must not block a new one
    fp_unique = fp if not session.execute(
        select(ManualReview.id).where(ManualReview.fingerprint == fp)).first() \
        else fingerprint(fp, uuid.uuid4())
    review = ManualReview(project_id=project_id, reason_code=reason_code, details=details,
                          related_entity_type=entity_type, related_entity_id=entity_id,
                          workflow_run_id=workflow_run_id, fingerprint=fp_unique)
    session.add(review)
    session.flush()
    return review


def blocking_open_issue_count(session: Session, project_id: uuid.UUID) -> int:
    rows = session.execute(
        select(RequirementIssue.id).where(
            RequirementIssue.project_id == project_id, RequirementIssue.blocking.is_(True),
            RequirementIssue.status.in_([IssueStatus.open, IssueStatus.clarification_requested]))
    ).all()
    return len(rows)
