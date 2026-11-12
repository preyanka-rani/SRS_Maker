import threading
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.database.models import (
    CauseType,
    InputSource,
    ReqStatus,
    RequirementHistory,
    SourceEvidence,
    SourceType,
)
from app.domain.hashing import substantive_hash
from app.services import requirement_service as rs
from tests.helpers import make_project


def make_evidence(db, project, n=3) -> list[uuid.UUID]:
    src = InputSource(project_id=project.id, source_type=SourceType.upload, sha256=uuid.uuid4().hex * 2,
                      original_filename="x.txt", size_bytes=1)
    db.add(src)
    db.flush()
    ids = []
    for i in range(n):
        ev = SourceEvidence(project_id=project.id, input_source_id=src.id, seq=i + 1,
                            excerpt=f"excerpt {i}", content_sha256="0" * 64)
        db.add(ev)
        db.flush()
        ids.append(ev.id)
    return ids


DRAFT = {"req_type": "FR", "title": "Password reset", "statement": "Users shall reset passwords by email.",
         "priority": "must", "confidence": 0.9, "source_language": "en",
         "details": {"purpose": "Recover access", "main_flow": ["Request reset", "Open link"]},
         "acceptance_criteria": [{"title": "Happy path", "given": "a registered user",
                                  "when": "they request a reset", "then": "an email is sent"}]}


def _create(db, project, ev, draft=None, **kw):
    return rs.create_requirement(db, project.id, draft or DRAFT, ev[:1],
                                 cause_type=CauseType.input_source, **kw)


def test_codes_follow_prefix_conventions_and_are_sequential(db):
    p = make_project(db)
    from app.database.models import ReqType

    assert [rs.allocate_code(db, p.id, ReqType.FR) for _ in range(3)] == ["FR-001", "FR-002", "FR-003"]
    assert rs.allocate_code(db, p.id, ReqType.NFR, "perf") == "NFR-PERF-001"
    assert rs.allocate_code(db, p.id, ReqType.NFR, "sec") == "NFR-SEC-001"
    assert rs.allocate_code(db, p.id, ReqType.SEC) == "SEC-001"
    assert rs.allocate_code(db, p.id, ReqType.FR) == "FR-004"  # never reused


def test_concurrent_allocation_never_collides(db, engine):
    p = make_project(db)
    db.commit()
    codes, errors = [], []

    def worker():
        try:
            with Session(engine) as s:
                from app.database.models import ReqType

                for _ in range(8):
                    codes.append(rs.allocate_code(s, p.id, ReqType.FR))
                    s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(codes) == 32 and len(set(codes)) == 32


def test_create_writes_children_evidence_and_history(db):
    p = make_project(db)
    ev = make_evidence(db, p)
    req = _create(db, p, ev)
    assert (req.code, req.content_revision, req.history_seq, req.status) == ("FR-001", 1, 1, ReqStatus.proposed)
    snap = rs.snapshot(db, req)
    assert snap["details"]["purpose"] == "Recover access"
    assert snap["acceptance_criteria"][0]["then"] == "an email is sent"
    assert snap["evidence_link_ids"] == [f"{ev[0]}:origin"]
    hist = db.execute(select(RequirementHistory).where(RequirementHistory.requirement_id == req.id)).scalars().all()
    assert len(hist) == 1 and hist[0].change_type.value == "created"


def test_modify_bumps_revisions_and_repoints_evidence(db):
    p = make_project(db)
    ev = make_evidence(db, p)
    req = _create(db, p, ev)
    rs.modify_requirement(db, req, {"statement": "Users shall reset passwords by SMS or email."},
                          [ev[1]], cause_type=CauseType.feedback, cause_id="f1")
    assert (req.content_revision, req.history_seq) == (2, 2)
    rs.modify_requirement(db, req, {"statement": "Users shall reset passwords by SMS only."},
                          [ev[2]], cause_type=CauseType.feedback)
    assert req.content_revision == 3
    # evidence VALID AT each revision: origin persists; the previous wording's evidence is retired
    at = {r: sorted(e.relation.value + ":" + str(e.source_evidence_id)[:4]
                    for e in rs.evidence_at(db, req, r)) for r in (1, 2, 3)}
    short = lambda i: str(ev[i])[:4]  # noqa: E731
    assert at[1] == [f"origin:{short(0)}"]
    assert at[2] == sorted([f"origin:{short(0)}", f"modifies:{short(1)}"])
    assert at[3] == sorted([f"origin:{short(0)}", f"modifies:{short(2)}"])


def test_modify_replaces_acceptance_criteria_revision_ranged(db):
    p = make_project(db)
    req = _create(db, p, make_evidence(db, p))
    rs.modify_requirement(db, req, {"acceptance_criteria": [
        {"title": "New", "given": "g", "when": "w", "then": "t2"}]}, [], cause_type=CauseType.feedback)
    assert [c.then_text for c in rs.criteria_at(db, req, 1)] == ["an email is sent"]
    assert [c.then_text for c in rs.criteria_at(db, req, 2)] == ["t2"]


def test_modifying_a_confirmed_requirement_marks_it_modified(db):
    p = make_project(db)
    req = _create(db, p, make_evidence(db, p))
    rs.change_status(db, req, ReqStatus.confirmed, cause_type=CauseType.approval)
    rs.modify_requirement(db, req, {"title": "Password recovery"}, [], cause_type=CauseType.feedback)
    assert req.status == ReqStatus.modified


def test_confirming_changes_history_seq_but_not_content_or_substantive_hash(db):
    p = make_project(db)
    req = _create(db, p, make_evidence(db, p))
    before = substantive_hash([rs.snapshot(db, req)], {}, {})
    rs.change_status(db, req, ReqStatus.confirmed, cause_type=CauseType.approval)
    after = substantive_hash([rs.snapshot(db, req)], {}, {})
    assert (req.content_revision, req.history_seq) == (1, 2)
    assert before == after


def test_illegal_status_transition_rejected(db):
    p = make_project(db)
    req = _create(db, p, make_evidence(db, p))
    with pytest.raises(ValidationFailed):
        rs.change_status(db, req, ReqStatus.modified, cause_type=CauseType.staff_edit)


def test_deprecate_records_removal_evidence_and_is_idempotent(db):
    p = make_project(db)
    ev = make_evidence(db, p)
    req = _create(db, p, ev)
    rs.deprecate(db, req, [ev[1]], cause_type=CauseType.feedback)
    rs.deprecate(db, req, [ev[1]], cause_type=CauseType.feedback)  # no second history row
    assert req.status == ReqStatus.deprecated and req.history_seq == 2
    hist = db.execute(select(RequirementHistory.change_type).where(
        RequirementHistory.requirement_id == req.id).order_by(RequirementHistory.history_seq)).scalars().all()
    assert [h.value for h in hist] == ["created", "deprecated"]
    assert any(e.relation.value == "removes" for e in rs.evidence_at(db, req))


def test_history_is_immutable_and_complete_snapshots(db):
    p = make_project(db)
    req = _create(db, p, make_evidence(db, p))
    rs.modify_requirement(db, req, {"statement": "Changed statement text here."}, [],
                          cause_type=CauseType.staff_edit, actor_type="user", actor_id="u1")
    db.commit()
    first = db.execute(select(RequirementHistory).where(
        RequirementHistory.requirement_id == req.id, RequirementHistory.history_seq == 1)).scalar_one()
    assert first.snapshot["statement"] == DRAFT["statement"]  # the past is reproducible
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE requirement_history SET change_type='modified'"))
    db.rollback()


def test_cross_project_evidence_link_is_rejected_by_composite_fk(db):
    a, b = make_project(db), make_project(db)
    ev_b = make_evidence(db, b)
    with pytest.raises(DBAPIError):
        rs.create_requirement(db, a.id, DRAFT, ev_b[:1], cause_type=CauseType.input_source)
    db.rollback()
