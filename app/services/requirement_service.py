"""Requirement persistence: stable IDs, revision-aware children/evidence, append-only history.

Two counters per requirement:
  content_revision - changes only when substantive content changes
  history_seq      - changes on EVERY change, including status-only ones
Status changes therefore never alter the substantive hash.
"""
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.database.models import (
    AcceptanceCriterion,
    CauseType,
    ChangeType,
    EvidenceRelation,
    FunctionalDetails,
    Priority,
    ReqStatus,
    Requirement,
    RequirementEvidence,
    RequirementHistory,
    RequirementIdCounter,
    ReqType,
)
from app.domain.hashing import statement_hash
from app.domain.lifecycle import format_code, id_prefix, require_transition

_DETAIL_FIELDS = ("purpose", "actors", "trigger", "preconditions", "main_flow", "alternative_flows",
                  "exception_flows", "business_rule_refs", "authorization", "state_transition",
                  "notifications_text", "audit_text", "related_ui", "related_data", "source_summary")
_CONTENT_FIELDS = ("title", "statement", "rationale", "priority", "category", "module_id")


def _priority(value: Any) -> Priority | None:
    if value is None or isinstance(value, Priority):
        return value
    return Priority(value)


def allocate_code(session: Session, project_id: uuid.UUID, req_type: ReqType,
                  category: str | None = None) -> str:
    """Next stable ID for (project, prefix). Row-locked so concurrent writers never collide."""
    prefix = id_prefix(req_type, category)
    session.execute(text(
        "INSERT INTO requirement_id_counters (project_id, prefix, next_value) "
        "VALUES (:p, :x, 1) ON CONFLICT DO NOTHING"), {"p": project_id, "x": prefix})
    counter = session.execute(
        select(RequirementIdCounter).where(RequirementIdCounter.project_id == project_id,
                                           RequirementIdCounter.prefix == prefix).with_for_update()
    ).scalar_one()
    number = counter.next_value
    counter.next_value = number + 1
    session.flush()
    return format_code(prefix, number)


def get_by_code(session: Session, project_id: uuid.UUID, code: str) -> Requirement:
    req = session.execute(select(Requirement).where(
        Requirement.project_id == project_id, Requirement.code == code)).scalar_one_or_none()
    if req is None:
        raise NotFoundError(f"Requirement {code} not found")
    return req


def _ac_valid(rev: int):
    return (AcceptanceCriterion.introduced_in_revision <= rev) & (
        AcceptanceCriterion.retired_in_revision.is_(None)
        | (AcceptanceCriterion.retired_in_revision > rev))


def _ev_valid(rev: int):
    return (RequirementEvidence.introduced_in_revision <= rev) & (
        RequirementEvidence.retired_in_revision.is_(None)
        | (RequirementEvidence.retired_in_revision > rev))


def evidence_at(session: Session, req: Requirement, revision: int | None = None) -> list[RequirementEvidence]:
    rev = revision or req.content_revision
    return list(session.execute(
        select(RequirementEvidence).where(RequirementEvidence.requirement_id == req.id,
                                          _ev_valid(rev))
        .order_by(RequirementEvidence.created_at, RequirementEvidence.id)).scalars())


def criteria_at(session: Session, req: Requirement, revision: int | None = None) -> list[AcceptanceCriterion]:
    rev = revision or req.content_revision
    return list(session.execute(
        select(AcceptanceCriterion).where(AcceptanceCriterion.requirement_id == req.id, _ac_valid(rev))
        .order_by(AcceptanceCriterion.seq)).scalars())


def snapshot(session: Session, req: Requirement) -> dict[str, Any]:
    """Everything that defines the requirement at its current revision."""
    details = session.get(FunctionalDetails, req.id)
    return {
        "code": req.code, "req_type": req.req_type.value, "category": req.category,
        "module_id": str(req.module_id) if req.module_id else None, "title": req.title,
        "statement": req.statement, "rationale": req.rationale,
        "priority": req.priority.value if req.priority else None,
        "status": req.status.value, "content_revision": req.content_revision,
        "history_seq": req.history_seq, "source_language": req.source_language,
        "details": {f: getattr(details, f) for f in _DETAIL_FIELDS} if details else {},
        "acceptance_criteria": [
            {"seq": a.seq, "title": a.scenario_title, "given": a.given_text, "when": a.when_text,
             "then": a.then_text, "test_case_code": a.test_case_code}
            for a in criteria_at(session, req)],
        "notification_specs": [],
        "evidence_link_ids": sorted(f"{e.source_evidence_id}:{e.relation.value}"
                                    for e in evidence_at(session, req)),
    }


def _record_history(session: Session, req: Requirement, change: ChangeType, cause: CauseType,
                    cause_id: str | None, actor_type: str, actor_id: str | None,
                    run_id: uuid.UUID | None, diff: dict | None = None) -> RequirementHistory:
    entry = RequirementHistory(
        project_id=req.project_id, requirement_id=req.id, history_seq=req.history_seq,
        content_revision=req.content_revision, change_type=change, snapshot=snapshot(session, req),
        diff=diff, cause_type=cause, cause_id=cause_id, actor_type=actor_type, actor_id=actor_id,
        workflow_run_id=run_id)
    session.add(entry)
    session.flush()
    return entry


def _set_details(session: Session, req: Requirement, details: dict | None) -> None:
    if not details:
        return
    row = session.get(FunctionalDetails, req.id)
    if row is None:
        row = FunctionalDetails(requirement_id=req.id, project_id=req.project_id)
        session.add(row)
    for field in _DETAIL_FIELDS:
        if field in details:
            setattr(row, field, details[field])
    session.flush()


def _add_criteria(session: Session, req: Requirement, criteria: list[dict], revision: int) -> None:
    for seq, c in enumerate(criteria, start=1):
        session.add(AcceptanceCriterion(
            project_id=req.project_id, requirement_id=req.id, seq=seq,
            scenario_title=c.get("title") or c.get("scenario_title"),
            given_text=c["given"], when_text=c["when"], then_text=c["then"],
            test_case_code=c.get("test_case_code"), introduced_in_revision=revision))
    session.flush()


def create_requirement(session: Session, project_id: uuid.UUID, draft: dict, evidence_ids: list[uuid.UUID],
                       *, cause_type: CauseType, cause_id: str | None = None,
                       actor_type: str = "system", actor_id: str | None = None,
                       run_id: uuid.UUID | None = None,
                       status: ReqStatus = ReqStatus.proposed) -> Requirement:
    req_type = ReqType(draft["req_type"])
    code = allocate_code(session, project_id, req_type, draft.get("category"))
    req = Requirement(
        project_id=project_id, code=code, req_type=req_type, category=draft.get("category"),
        module_id=draft.get("module_id"), title=draft["title"], statement=draft["statement"],
        rationale=draft.get("rationale"), source_language=draft.get("source_language"),
        priority=_priority(draft.get("priority")), status=status, content_revision=1, history_seq=1,
        normalized_hash=statement_hash(draft["statement"]), ai_confidence=draft.get("confidence"))
    session.add(req)
    session.flush()
    _set_details(session, req, draft.get("details"))
    _add_criteria(session, req, draft.get("acceptance_criteria", []), 1)
    for ev in evidence_ids:
        session.add(RequirementEvidence(project_id=project_id, requirement_id=req.id,
                                        source_evidence_id=ev, relation=EvidenceRelation.origin,
                                        introduced_in_revision=1))
    session.flush()
    _record_history(session, req, ChangeType.created, cause_type, cause_id, actor_type, actor_id, run_id)
    return req


def modify_requirement(session: Session, req: Requirement, changes: dict, evidence_ids: list[uuid.UUID],
                       *, cause_type: CauseType, cause_id: str | None = None,
                       actor_type: str = "system", actor_id: str | None = None,
                       run_id: uuid.UUID | None = None) -> Requirement:
    """Substantive change: content_revision+1, history_seq+1, evidence re-pointed.

    A confirmed requirement becomes `modified`; proposed/needs_clarification keep their status.
    """
    before = snapshot(session, req)
    new_rev = req.content_revision + 1
    for field in _CONTENT_FIELDS:
        if field in changes and changes[field] is not None:
            value = _priority(changes[field]) if field == "priority" else changes[field]
            setattr(req, field, value)
    if "statement" in changes and changes["statement"]:
        req.normalized_hash = statement_hash(req.statement)
    if "details" in changes:
        _set_details(session, req, changes["details"])
    if "acceptance_criteria" in changes and changes["acceptance_criteria"] is not None:
        for old in criteria_at(session, req, req.content_revision):
            old.retired_in_revision = new_rev
        session.flush()
        _add_criteria(session, req, changes["acceptance_criteria"], new_rev)
    # evidence for the previous wording is retired; origin links (provenance) stay
    for link in evidence_at(session, req, req.content_revision):
        if link.relation == EvidenceRelation.modifies:
            link.retired_in_revision = new_rev
    for ev in evidence_ids:
        session.add(RequirementEvidence(project_id=req.project_id, requirement_id=req.id,
                                        source_evidence_id=ev, relation=EvidenceRelation.modifies,
                                        introduced_in_revision=new_rev))
    if req.status == ReqStatus.confirmed:
        req.status = ReqStatus.modified
    req.content_revision = new_rev
    req.history_seq += 1
    req.lock_version += 1
    session.flush()
    after = snapshot(session, req)
    diff = {k: {"from": before[k], "to": after[k]} for k in after if before.get(k) != after[k]
            and k not in ("history_seq",)}
    _record_history(session, req, ChangeType.modified, cause_type, cause_id, actor_type, actor_id,
                    run_id, diff)
    return req


def change_status(session: Session, req: Requirement, new_status: ReqStatus, *,
                  cause_type: CauseType, cause_id: str | None = None, actor_type: str = "system",
                  actor_id: str | None = None, run_id: uuid.UUID | None = None) -> Requirement:
    """Status-only change: history_seq+1, content_revision UNCHANGED (substantive hash stable)."""
    if new_status == req.status:
        return req
    require_transition(req.status, new_status)
    old = req.status
    req.status = new_status
    req.history_seq += 1
    req.lock_version += 1
    session.flush()
    change = ChangeType.restored if old == ReqStatus.deprecated else ChangeType.status_changed
    _record_history(session, req, change, cause_type, cause_id, actor_type, actor_id, run_id,
                    {"status": {"from": old.value, "to": new_status.value}})
    return req


def deprecate(session: Session, req: Requirement, evidence_ids: list[uuid.UUID], *,
              cause_type: CauseType, cause_id: str | None = None, actor_type: str = "system",
              actor_id: str | None = None, run_id: uuid.UUID | None = None) -> Requirement:
    if req.status == ReqStatus.deprecated:
        return req
    require_transition(req.status, ReqStatus.deprecated)
    for ev in evidence_ids:
        session.add(RequirementEvidence(
            project_id=req.project_id, requirement_id=req.id, source_evidence_id=ev,
            relation=EvidenceRelation.removes, introduced_in_revision=req.content_revision))
    old = req.status
    req.status = ReqStatus.deprecated
    req.history_seq += 1
    req.lock_version += 1
    session.flush()
    _record_history(session, req, ChangeType.deprecated, cause_type, cause_id, actor_type, actor_id,
                    run_id, {"status": {"from": old.value, "to": "deprecated"}})
    return req


def list_requirements(session: Session, project_id: uuid.UUID,
                      include_deprecated: bool = True) -> list[Requirement]:
    query = select(Requirement).where(Requirement.project_id == project_id)
    if not include_deprecated:
        query = query.where(Requirement.status != ReqStatus.deprecated)
    return list(session.execute(query.order_by(Requirement.code)).scalars())


def snapshots(session: Session, project_id: uuid.UUID) -> list[dict]:
    return [snapshot(session, r) for r in list_requirements(session, project_id)]
