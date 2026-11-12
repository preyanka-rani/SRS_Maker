"""Approval: the model reports signals; eight deterministic checks decide; acceptance freezes
the approved version, confirms requirements (status only) and builds the verified final v1.0."""
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    Approval,
    CauseType,
    EmailMessage,
    EmailOutbox,
    InputSource,
    MatchMethod,
    Project,
    ProjectStatus,
    ReqStatus,
    SrsVersion,
    VersionStatus,
)
from app.domain import approval_rules as rules
from app.integrations.storage.local import LocalStorage
from app.llm.client import LlmError
from app.llm.output_schemas import ApprovalSignal
from app.llm.prompt_registry import load_prompt
from app.services import event_log, issue_service, outbox_service, project_service, srs_service
from app.services import requirement_service as rs
from app.services.llm_service import GuardedLlm

_VERSION_IN_TEXT = re.compile(r"\b(?:v(?:ersion)?\.?\s*)?(\d+\.\d+)\b", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _signal(session: Session, llm: GuardedLlm, email: EmailMessage) -> ApprovalSignal | None:
    prompt = load_prompt("approval_signal")
    try:
        result, _ = llm.run(
            purpose="approval_signal", prompt_id=prompt.prompt_id, prompt_version=prompt.version,
            system=prompt.text, instruction="Report the approval signals in the client's email.",
            sources=[{"ref": "M1", "text": email.new_content_text or "", "lang": None,
                      "origin": "client email"}], schema=ApprovalSignal)
    except LlmError:
        return None
    return result.parsed


def _referenced_version(session: Session, project: Project, email: EmailMessage,
                        signal: ApprovalSignal | None) -> tuple[SrsVersion | None, bool]:
    """Returns (version the client refers to, label_matches_when_token_bound)."""
    ids = ([email.in_reply_to] if email.in_reply_to else []) + list(email.references or [])
    if ids:
        row = session.execute(select(EmailOutbox.srs_version_id).where(
            EmailOutbox.message_id.in_(ids), EmailOutbox.srs_version_id.is_not(None))).first()
        if row:
            return session.get(SrsVersion, row[0]), True
    current = session.get(SrsVersion, project.current_srs_version_id) if project.current_srs_version_id else None
    stated = signal.states_version if signal and signal.states_version else None
    if stated is None:
        m = _VERSION_IN_TEXT.search(email.subject or "")
        stated = m.group(1) if m else None
    return current, bool(current and stated and stated == current.version_label)


def _integrity_ok(storage: LocalStorage, version: SrsVersion) -> bool:
    try:
        return (storage.sha256(version.docx_path) == version.docx_sha256
                and storage.sha256(version.pdf_path) == version.pdf_sha256)
    except (OSError, ValueError, TypeError):
        return False


def _sender_is_approver(session: Session, project_id: uuid.UUID, address: str | None) -> uuid.UUID | None:
    for contact, pc in project_service.contacts_for_project(session, project_id):
        if pc.can_approve and address and contact.email.lower() == address.lower():
            return contact.id
    return None


def validate(session: Session, storage: LocalStorage, settings: Settings, llm: GuardedLlm,
             project: Project, email: EmailMessage, run_id: uuid.UUID | None
             ) -> tuple[rules.ApprovalDecision, SrsVersion | None, uuid.UUID | None, ApprovalSignal | None]:
    signal = _signal(session, llm, email)
    version, label_ok = _referenced_version(session, project, email, signal)
    contact_id = _sender_is_approver(session, project.id, email.from_address)
    current = (version is not None and version.id == project.current_srs_version_id
               and version.status == VersionStatus.sent)
    has_attachments = session.execute(select(InputSource.id).where(
        InputSource.email_message_id == email.id).limit(1)).first() is not None
    facts = rules.ApprovalFacts(
        sender_is_approver=contact_id is not None, sender_auth=email.sender_auth_verdict.value,
        require_sender_auth=settings.require_sender_auth_for_approval,
        binding="headers" if email.match_method == MatchMethod.headers else
        "subject_token_sender" if email.match_method == MatchMethod.subject_token_sender else "none",
        version_label_matches=label_ok, version_is_current=current,
        explicit_approval=bool(signal and signal.explicit_approval),
        approval_covers_all=bool(signal and signal.approval_scope_all_requirements),
        signal_confidence=signal.confidence if signal else 0.0,
        min_confidence=settings.feedback_min_confidence,
        has_change_requests=bool(signal and signal.has_change_requests),
        has_questions=bool(signal and signal.has_questions), has_attachments=has_attachments,
        open_blockers=issue_service.blocking_open_issue_count(session, project.id),
        integrity_ok=bool(version and _integrity_ok(storage, version)))
    return rules.evaluate(facts), version, contact_id, signal


def _record(session: Session, project: Project, email: EmailMessage, version: SrsVersion | None,
            contact_id, decision: rules.ApprovalDecision, signal: ApprovalSignal | None) -> Approval | None:
    if version is None:
        return None
    existing = session.execute(select(Approval).where(Approval.email_message_id == email.id)).scalar_one_or_none()
    if existing:
        return existing
    row = Approval(
        project_id=project.id, srs_version_id=version.id, email_message_id=email.id,
        contact_id=contact_id, outcome=decision.outcome,
        checks={"checks": decision.checks, "reasons": list(decision.reasons),
                "match_method": email.match_method.value},
        evidence_excerpt=(signal.quoted_phrase if signal else None),
        approved_substantive_hash=version.substantive_hash, approved_content_hash=version.content_hash,
        approved_docx_sha256=version.docx_sha256, approved_pdf_sha256=version.pdf_sha256)
    session.add(row)
    session.flush()
    return row


def _refusal_email(session, settings, project, email, decision) -> None:
    reasons = "\n".join(f"  - {r}" for r in decision.reasons)
    body = ("Hello,\n\nThank you for your message. We could not treat it as an approval of the "
            f"current SRS because:\n{reasons}\n\nPlease reply to the latest SRS email from an "
            "authorised approver's address, stating that you approve that version with no further "
            f"changes.\n\nRegards,\n{settings.smtp_from_name}")
    outbox_service.enqueue(
        session, settings, project, kind="approval_refusal", to=[email.from_address],
        subject=outbox_service.subject_for(project, "approval could not be recorded"), body_text=body,
        idempotency_key=f"refusal:{email.id}", in_reply_to=email.message_id,
        references=[*(email.references or []), email.message_id])


def accept(session: Session, storage: LocalStorage, settings: Settings, project: Project,
           email: EmailMessage, approved: SrsVersion, approval: Approval, signal: ApprovalSignal | None,
           run_id: uuid.UUID | None) -> SrsVersion | None:
    """Freeze the approved version, confirm requirements (status only), build + verify v1.0."""
    approved.status, approved.approved_at = VersionStatus.approved, _now()
    for req in rs.list_requirements(session, project.id, include_deprecated=False):
        if req.status in (ReqStatus.proposed, ReqStatus.modified):
            rs.change_status(session, req, ReqStatus.confirmed, cause_type=CauseType.approval,
                             cause_id=str(approval.id), run_id=run_id)
    project.status = ProjectStatus.approved
    session.flush()
    approver = email.from_address or "client"
    text = (f"Approved by {approver} on {_now().strftime('%Y-%m-%d')}: "
            f"\"{(signal.quoted_phrase if signal and signal.quoted_phrase else 'approved')}\"")
    final = srs_service.finalize(session, storage, settings, project, approved, text, approver, run_id)
    if final is None:
        return None
    approval.final_srs_version_id = final.id
    srs_service.queue_final_email(session, storage, settings, project, final)
    event_log.log_event(session, project.id, "approval_accepted", ref_type="approval",
                        ref_id=str(approval.id), run_id=run_id)
    return final


def process_approval(session: Session, storage: LocalStorage, settings: Settings, llm: GuardedLlm,
                     project: Project, email: EmailMessage, run_id: uuid.UUID | None) -> str:
    """Returns accepted | refused | revision | manual_review."""
    decision, version, contact_id, signal = validate(session, storage, settings, llm, project, email, run_id)
    approval = _record(session, project, email, version, contact_id, decision, signal)
    event_log.log_event(session, project.id, "approval_evaluated", ref_type="email", ref_id=str(email.id),
                        run_id=run_id, details={"outcome": decision.outcome, "checks": decision.checks})
    if decision.outcome == rules.ACCEPTED and approval is not None and version is not None:
        final = accept(session, storage, settings, project, email, version, approval, signal, run_id)
        return rules.ACCEPTED if final else rules.MANUAL
    if decision.outcome == rules.REFUSED:
        _refusal_email(session, settings, project, email, decision)
    elif decision.outcome == rules.MANUAL:
        reason = "THREAD_BINDING_FAILED" if not decision.checks["3_thread_bound"] else "APPROVAL_UNCERTAIN"
        issue_service.open_manual_review(session, project.id, reason, details="; ".join(decision.reasons),
                                         entity_type="email", entity_id=str(email.id),
                                         workflow_run_id=run_id)
    return decision.outcome


__all__ = ["process_approval", "accept", "validate"]
