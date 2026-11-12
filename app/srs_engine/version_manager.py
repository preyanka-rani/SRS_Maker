"""SRS version bookkeeping: labels, sequence numbers, hashes, supersession, change summaries.

Artifact columns are written while a version is `generating`; the database triggers freeze them
afterwards, so this module only ever changes lifecycle columns on existing versions.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import (
    IssueStatus,
    IssueType,
    Project,
    RequirementIssue,
    SrsVersion,
    SrsVersionRequirement,
    VersionKind,
    VersionStatus,
)
from app.domain.hashing import input_fingerprint, requirement_set_hash, sha256_json, substantive_hash
from app.services import requirement_service as rs

LIVE_STATUSES = (VersionStatus.ready, VersionStatus.sent, VersionStatus.approved)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def live_snapshots(session: Session, project_id: uuid.UUID) -> list[dict]:
    return [s for s in rs.snapshots(session, project_id) if s["status"] != "deprecated"]


def issues_hash(session: Session, project_id: uuid.UUID) -> str:
    rows = session.execute(select(RequirementIssue.fingerprint, RequirementIssue.status,
                                  RequirementIssue.blocking).where(
        RequirementIssue.project_id == project_id)).all()
    return sha256_json(sorted((r[0], r[1].value, bool(r[2])) for r in rows))


def compute_hashes(session: Session, project: Project, template_version: int,
                   prompt_versions: dict) -> dict[str, str]:
    snaps = rs.snapshots(session, project.id)
    metadata = {"title": project.title, "subtitle": project.subtitle, "code": project.code}
    set_hash = requirement_set_hash(snaps, {}, metadata)
    return {
        "substantive_hash": substantive_hash(snaps, {}, metadata),
        "requirement_set_hash": set_hash,
        "input_fingerprint": input_fingerprint(set_hash, issues_hash(session, project.id),
                                               template_version, prompt_versions),
    }


def latest_live(session: Session, project_id: uuid.UUID) -> SrsVersion | None:
    return session.execute(
        select(SrsVersion).where(SrsVersion.project_id == project_id,
                                 SrsVersion.kind == VersionKind.draft,
                                 SrsVersion.status.in_(LIVE_STATUSES))
        .order_by(SrsVersion.seq.desc()).limit(1)).scalar_one_or_none()


def next_seq(session: Session, project_id: uuid.UUID) -> int:
    return (session.execute(select(func.max(SrsVersion.seq)).where(
        SrsVersion.project_id == project_id)).scalar() or 0) + 1


def next_draft_label(session: Session, project_id: uuid.UUID) -> str:
    """0.1, 0.2, ... A failed attempt never consumes a label (its label is released)."""
    labels = session.execute(select(SrsVersion.version_label).where(
        SrsVersion.project_id == project_id, SrsVersion.kind == VersionKind.draft,
        SrsVersion.status.notin_([VersionStatus.failed, VersionStatus.qa_failed]))).scalars().all()
    minors = [int(lbl.split(".")[1]) for lbl in labels if lbl.startswith("0.") and lbl.split(".")[1].isdigit()]
    return f"0.{max(minors, default=0) + 1}"


def abandon_stale_generating(session: Session, project_id: uuid.UUID) -> None:
    for version in session.execute(select(SrsVersion).where(
            SrsVersion.project_id == project_id,
            SrsVersion.status == VersionStatus.generating)).scalars():
        version.status = VersionStatus.failed
        version.error = "abandoned: a previous generation did not finish"
    session.flush()


def supersede_older(session: Session, project_id: uuid.UUID, keep_id: uuid.UUID) -> None:
    for version in session.execute(select(SrsVersion).where(
            SrsVersion.project_id == project_id, SrsVersion.id != keep_id,
            SrsVersion.kind == VersionKind.draft,
            SrsVersion.status.in_([VersionStatus.ready, VersionStatus.sent]))).scalars():
        version.status = VersionStatus.superseded
        version.superseded_at = _now()
    session.flush()


def pin_requirements(session: Session, version: SrsVersion, snaps: list[dict]) -> None:
    by_code = {r.code: r for r in rs.list_requirements(session, version.project_id)}
    for snap in snaps:
        req = by_code[snap["code"]]
        session.add(SrsVersionRequirement(
            srs_version_id=version.id, requirement_id=req.id, project_id=version.project_id,
            content_revision=snap["content_revision"], status_at_snapshot=snap["status"]))
    session.flush()


def pinned_revisions(session: Session, version_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = session.execute(select(SrsVersionRequirement.requirement_id,
                                  SrsVersionRequirement.content_revision).where(
        SrsVersionRequirement.srs_version_id == version_id)).all()
    return {r[0]: r[1] for r in rows}


def change_summary(session: Session, previous: SrsVersion | None, snaps: list[dict]) -> str:
    """Diff of pinned requirements against the previous version."""
    if previous is None:
        return f"Initial draft with {len(snaps)} requirement(s)."
    old = pinned_revisions(session, previous.id)
    by_code = {r.code: r for r in rs.list_requirements(session, previous.project_id)}
    added, changed = [], []
    for snap in snaps:
        req = by_code[snap["code"]]
        if req.id not in old:
            added.append(snap["code"])
        elif old[req.id] != snap["content_revision"]:
            changed.append(snap["code"])
    live_ids = {by_code[s["code"]].id for s in snaps}
    removed = [by_code_id for by_code_id in old if by_code_id not in live_ids]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if changed:
        parts.append("changed " + ", ".join(changed))
    if removed:
        parts.append(f"removed {len(removed)} requirement(s)")
    return ("Revision: " + "; ".join(parts) + ".") if parts else "Revision: clarifications only."


def open_questions(session: Session, project_id: uuid.UUID) -> list[str]:
    """Everything still open for the client: blocking items first, then follow-up questions."""
    rows = session.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project_id,
        RequirementIssue.status.in_([IssueStatus.open, IssueStatus.clarification_requested]),
        RequirementIssue.issue_type != IssueType.injection_suspected)
        .order_by(RequirementIssue.blocking.desc(), RequirementIssue.created_at).limit(15)).scalars().all()
    return [i.title for i in rows]


def blocking_questions(session: Session, project_id: uuid.UUID) -> list[str]:
    rows = session.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project_id, RequirementIssue.blocking.is_(True),
        RequirementIssue.status.in_([IssueStatus.open, IssueStatus.clarification_requested]))
        .order_by(RequirementIssue.created_at).limit(15)).scalars().all()
    return [f"{i.title}" for i in rows]
