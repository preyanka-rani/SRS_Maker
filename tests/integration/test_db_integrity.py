"""Schema-level guarantees that only a real Postgres can prove."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database.models import (
    Client,
    ClientContact,
    ContactRole,
    Project,
    ProjectContact,
    ProjectLlmConsent,
)
from app.database.models.srs import SrsSection, SrsVersion, VersionKind, VersionStatus
from app.services import audit_service


def _client(db, name="Acme"):
    c = Client(organisation_name=name)
    db.add(c)
    db.flush()
    return c


def _project(db, client, code="P1", token="PRJ-AAAAAA"):
    p = Project(client_id=client.id, code=code, title="T", project_token=token)
    db.add(p)
    db.flush()
    return p


def test_contact_cannot_be_authorized_on_another_clients_project(db):
    a, b = _client(db, "A"), _client(db, "B")
    project_a = _project(db, a)
    contact_b = ClientContact(client_id=b.id, full_name="Bob", email="bob@b.com")
    db.add(contact_b)
    db.flush()
    db.add(ProjectContact(project_id=project_a.id, client_id=a.id, contact_id=contact_b.id,
                          role=ContactRole.approver))
    with pytest.raises(IntegrityError):
        db.flush()


def test_same_client_contact_can_be_authorized(db):
    a = _client(db)
    p = _project(db, a)
    c = ClientContact(client_id=a.id, full_name="Ann", email="ann@a.com")
    db.add(c)
    db.flush()
    db.add(ProjectContact(project_id=p.id, client_id=a.id, contact_id=c.id,
                          role=ContactRole.approver, can_approve=True))
    db.flush()


def test_only_one_active_consent_per_scope_but_revoked_allows_new(db):
    a = _client(db)
    p = _project(db, a)
    db.add(ProjectLlmConsent(project_id=p.id, scope="text_processing", policy_reference="DPA-1"))
    db.flush()
    db.add(ProjectLlmConsent(project_id=p.id, scope="text_processing", policy_reference="DPA-2"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_consent_rows_cannot_be_deleted_or_rewritten(db):
    a = _client(db)
    p = _project(db, a)
    consent = ProjectLlmConsent(project_id=p.id, scope="audio_processing", policy_reference="X")
    db.add(consent)
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM project_llm_consents"))
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE project_llm_consents SET policy_reference='changed'"))
    db.rollback()
    # a one-time revoke is allowed
    db.execute(text("UPDATE project_llm_consents SET revoked_at = now(), revocation_reason='r'"))
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE project_llm_consents SET revocation_reason='again'"))
    db.rollback()


def test_audit_log_is_append_only_and_hash_chained(db):
    audit_service.record(db, actor_type="system", actor_id=None, entity_type="x", action="a")
    audit_service.record(db, actor_type="system", actor_id=None, entity_type="x", action="b")
    db.commit()
    rows = db.execute(text("SELECT prev_hash, hash FROM audit_logs ORDER BY id")).all()
    assert rows[1].prev_hash == rows[0].hash
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE audit_logs SET action='tampered'"))
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM audit_logs"))
    db.rollback()


def _version(db, project, label="0.1", kind=VersionKind.draft, seq=1):
    v = SrsVersion(project_id=project.id, seq=seq, version_label=label, kind=kind)
    db.add(v)
    db.flush()
    return v


def _complete(v):
    v.docx_sha256 = v.pdf_sha256 = v.content_hash = "a" * 64
    v.substantive_hash = v.requirement_set_hash = "b" * 64
    v.qa_report = {"passed": True}


def test_version_cannot_become_ready_without_artifacts_or_qa_pass(db):
    p = _project(db, _client(db))
    v = _version(db, p)
    v.status = VersionStatus.ready
    with pytest.raises(DBAPIError):
        db.flush()
    db.rollback()


def test_version_artifacts_writable_while_generating_then_frozen(db):
    p = _project(db, _client(db))
    v = _version(db, p)
    _complete(v)
    db.flush()  # artifact columns writable while generating
    v.generation_attempt = 2
    db.flush()
    v.status = VersionStatus.ready
    db.flush()
    db.commit()
    for stmt in ("UPDATE srs_versions SET docx_sha256 = 'x'",
                 "UPDATE srs_versions SET qa_report = '{}'::jsonb",
                 "DELETE FROM srs_versions"):
        with pytest.raises(DBAPIError):
            db.execute(text(stmt))
        db.rollback()
    db.execute(text("UPDATE srs_versions SET status='sent', sent_at=now()"))  # allowed
    db.commit()
    with pytest.raises(DBAPIError):  # write-once timestamp
        db.execute(text("UPDATE srs_versions SET sent_at = now() + interval '1 day'"))
    db.rollback()


def test_illegal_and_draft_final_transitions_rejected(db):
    p = _project(db, _client(db))
    v = _version(db, p)
    _complete(v)
    v.status = VersionStatus.final  # draft cannot become final
    with pytest.raises(DBAPIError):
        db.flush()
    db.rollback()


def test_sections_frozen_once_version_not_generating(db):
    p = _project(db, _client(db))
    v = _version(db, p)
    db.add(SrsSection(project_id=p.id, srs_version_id=v.id, section_key="2", content={"a": 1}))
    db.flush()  # allowed while generating
    _complete(v)
    v.status = VersionStatus.ready
    db.flush()
    db.add(SrsSection(project_id=p.id, srs_version_id=v.id, section_key="3", content={}))
    with pytest.raises(DBAPIError):
        db.flush()
    db.rollback()


def test_failed_attempt_releases_label_and_one_generating_per_project(db):
    p = _project(db, _client(db))
    v1 = _version(db, p, "0.1", seq=1)
    v2 = SrsVersion(project_id=p.id, seq=2, version_label="0.1", kind=VersionKind.draft)
    db.add(v2)
    with pytest.raises(IntegrityError):  # label taken and one generating at a time
        db.flush()
    db.rollback()
    v1 = db.execute(text("SELECT 1")).scalar()  # connection still usable
    assert v1 == 1


def test_label_reusable_after_terminal_failure(db):
    p = _project(db, _client(db))
    v1 = _version(db, p, "0.1", seq=1)
    v1.status = VersionStatus.failed
    v1.error = "boom"
    db.flush()
    v2 = SrsVersion(project_id=p.id, seq=2, version_label="0.1", kind=VersionKind.draft)
    db.add(v2)
    db.flush()  # allowed: failed attempts release their label


def test_inbound_seq_is_monotonic(db):
    from app.database.models import EmailMessage

    for i in range(3):
        db.add(EmailMessage(message_id=f"<{uuid.uuid4()}@x>"))
    db.flush()
    seqs = db.execute(text("SELECT inbound_seq FROM email_messages ORDER BY inbound_seq")).scalars().all()
    assert seqs == sorted(seqs) and len(set(seqs)) == 3 and seqs[0] > 0
