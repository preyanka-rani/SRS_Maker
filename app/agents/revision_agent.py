"""Client email feedback: classify, apply changes as new requirement revisions, answer questions.

The model proposes; code validates every target code against the project's real requirements
and applies changes through requirement_service (history + revision-aware evidence).
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    CauseType,
    EmailMessage,
    ExtractionStatus,
    FeedbackCategory,
    FeedbackChangeItem,
    FeedbackRecord,
    InputSource,
    IssueType,
    MailStatus,
    Project,
    ReqStatus,
    SourceEvidence,
    SourceType,
)
from app.integrations.storage.local import LocalStorage
from app.llm.client import LlmError
from app.llm.output_schemas import ChangeItemDraft, FeedbackOutput, ReplyDraft
from app.llm.prompt_registry import load_prompt
from app.services import (
    event_log,
    extraction_service,
    ingestion_service,
    issue_service,
    outbox_service,
    project_service,
)
from app.services import requirement_service as rs
from app.services.llm_service import GuardedLlm

CHANGE_CATEGORIES = {"NEW_REQUIREMENT", "MODIFY_REQUIREMENT", "REMOVE_REQUIREMENT",
                     "REQUIREMENT_CONFIRMATION"}
QUESTION_CATEGORIES = {"CLARIFICATION_REQUEST", "GENERAL_QUESTION"}


@dataclass(frozen=True)
class FeedbackResult:
    route: str  # approval | changed | question | manual | ignored
    feedback_id: uuid.UUID | None = None
    changed_codes: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _email_evidence(session: Session, storage: LocalStorage, settings: Settings, project_id: uuid.UUID,
                    email: EmailMessage) -> list[uuid.UUID]:
    """Make the client's new text citable: one text source + verbatim evidence rows."""
    text = (email.new_content_text or "").strip()
    if not text:
        return []
    res = ingestion_service.register_source(
        session, storage, settings, project_id, text.encode("utf-8"), "email-reply.txt",
        SourceType.email_body, mime="text/plain", email_message_id=email.id)
    source = ingestion_service.ingest_text_source(session, storage, settings, res.source.id)
    source.consumed_by_workflow_at = _now()
    return list(session.execute(select(SourceEvidence.id).where(
        SourceEvidence.input_source_id == source.id).order_by(SourceEvidence.seq)).scalars())


def _extract_from_attachments(session, llm: GuardedLlm, project_id, email, run_id) -> list[str]:
    """Text attachments on a reply are requirement sources like any other."""
    sources = session.execute(select(InputSource).where(
        InputSource.email_message_id == email.id, InputSource.source_type == SourceType.email_attachment,
        InputSource.extraction_status == ExtractionStatus.succeeded,
        InputSource.consumed_by_workflow_at.is_(None))).scalars().all()
    codes: list[str] = []
    for source in sources:
        report = extraction_service.ExtractionReport()
        cands = extraction_service.extract_candidates(session, llm, project_id, [source.id], run_id, report)
        extraction_service.merge_candidates(session, llm, project_id, cands, run_id, report)
        source.consumed_by_workflow_at = _now()
        codes.append(source.original_filename or "attachment")
    return codes


def _requirement_listing(session: Session, project_id: uuid.UUID) -> list[dict]:
    return [{"code": r.code, "title": r.title, "statement": r.statement}
            for r in rs.list_requirements(session, project_id, include_deprecated=False)]


def _classify(session: Session, llm: GuardedLlm, project_id: uuid.UUID, email: EmailMessage,
              listing: list[dict]) -> tuple[FeedbackOutput, uuid.UUID | None]:
    prompt = load_prompt("classify_feedback")
    result, _ = llm.run(
        purpose="classify_feedback", prompt_id=prompt.prompt_id, prompt_version=prompt.version,
        system=prompt.text,
        instruction="Classify the client's email. Valid requirement codes: "
                    + (", ".join(r["code"] for r in listing) or "(none yet)"),
        sources=[{"ref": "M1", "text": email.new_content_text or "", "lang": None, "origin": "client email"},
                 {"ref": "R1", "text": str(listing), "lang": "en", "origin": "current requirements"}],
        schema=FeedbackOutput)
    return result.parsed, None


def _apply_item(session: Session, project_id: uuid.UUID, item: ChangeItemDraft, record: FeedbackRecord,
                evidence_ids: list[uuid.UUID], run_id: uuid.UUID | None) -> str | None:
    """Returns the affected requirement code, or None when nothing was applied."""
    valid_codes = {r.code for r in rs.list_requirements(session, project_id)}
    row = FeedbackChangeItem(project_id=project_id, feedback_record_id=record.id, action=item.action,
                             proposed_title=item.title, proposed_statement=item.statement,
                             req_type=item.req_type, evidence_excerpt=item.evidence_excerpt,
                             status="proposed")
    session.add(row)
    session.flush()
    if item.action == "add" and item.statement and item.req_type and item.title:
        req = rs.create_requirement(
            session, project_id,
            {"req_type": item.req_type, "category": item.category, "title": item.title,
             "statement": item.statement, "confidence": None}, evidence_ids, cause_type=CauseType.feedback,
            cause_id=str(record.id), run_id=run_id)
        row.target_requirement_id, row.status = req.id, "applied"
        return req.code
    if item.target_code not in valid_codes:
        row.status = "needs_target"
        return None
    req = rs.get_by_code(session, project_id, item.target_code)
    row.target_requirement_id = req.id
    if item.action == "modify" and item.statement:
        changes = {"statement": item.statement}
        if item.title:
            changes["title"] = item.title
        rs.modify_requirement(session, req, changes, evidence_ids, cause_type=CauseType.feedback,
                              cause_id=str(record.id), run_id=run_id)
    elif item.action == "remove":
        rs.deprecate(session, req, evidence_ids, cause_type=CauseType.feedback,
                     cause_id=str(record.id), run_id=run_id)
    elif item.action == "confirm":
        if req.status in (ReqStatus.proposed, ReqStatus.modified):
            rs.change_status(session, req, ReqStatus.confirmed, cause_type=CauseType.feedback,
                             cause_id=str(record.id), run_id=run_id)
    else:
        row.status = "noted"
        return None
    row.status = "applied"
    return req.code


def _ask_which(session: Session, settings: Settings, project: Project, email: EmailMessage,
               unresolved: list[ChangeItemDraft]) -> None:
    lines = "\n".join(f"  - {i.statement or i.title or i.evidence_excerpt}" for i in unresolved)
    body = ("Hello,\n\nThank you for your reply. We could not tell which requirement the following "
            f"point refers to:\n{lines}\n\nCould you tell us the requirement code (for example FR-003) "
            "or describe it in more detail?\n\nRegards,\n" + settings.smtp_from_name)
    sender = email.from_address
    if sender:
        outbox_service.enqueue(
            session, settings, project, kind="answer", to=[sender],
            subject=outbox_service.subject_for(project, "a quick clarification"), body_text=body,
            idempotency_key=f"clarify:{email.id}", in_reply_to=email.message_id,
            references=[*(email.references or []), email.message_id])


def draft_answer(session: Session, settings: Settings, llm: GuardedLlm, project: Project,
                 email: EmailMessage, listing: list[dict]) -> None:
    prompt = load_prompt("draft_reply")
    try:
        result, _ = llm.run(
            purpose="draft_reply", prompt_id=prompt.prompt_id, prompt_version=prompt.version,
            system=prompt.text, instruction="Draft a reply to the client's question.",
            sources=[{"ref": "M1", "text": email.new_content_text or "", "lang": None, "origin": "client email"},
                     {"ref": "R1", "text": str(listing), "lang": "en", "origin": "current requirements"}],
            schema=ReplyDraft)
    except LlmError:
        issue_service.open_manual_review(session, project.id, "REPLY_NEEDED", entity_type="email",
                                         entity_id=str(email.id))
        return
    draft = result.parsed
    if draft.needs_human or settings.reply_requires_staff_review:
        issue_service.open_manual_review(session, project.id, "REPLY_REVIEW", details=draft.body,
                                         entity_type="email", entity_id=str(email.id))
        return
    body = f"Hello,\n\n{draft.body}\n\nRegards,\n{settings.smtp_from_name}"
    outbox_service.enqueue(
        session, settings, project, kind="answer", to=[email.from_address],
        subject=outbox_service.subject_for(project, "reply to your question"), body_text=body,
        idempotency_key=f"answer:{email.id}", in_reply_to=email.message_id,
        references=[*(email.references or []), email.message_id])


def analyze_email(session: Session, storage: LocalStorage, settings: Settings, llm: GuardedLlm,
                  project: Project, email: EmailMessage, run_id: uuid.UUID | None) -> FeedbackResult:
    existing = session.execute(select(FeedbackRecord).where(
        FeedbackRecord.email_message_id == email.id)).scalar_one_or_none()
    if existing is not None and existing.status == "processed":
        return FeedbackResult("ignored", existing.id)
    email.processing_status = MailStatus.processing
    _extract_from_attachments(session, llm, project.id, email, run_id)
    listing = _requirement_listing(session, project.id)
    try:
        output, _ = _classify(session, llm, project.id, email, listing)
    except LlmError as exc:
        issue_service.open_manual_review(session, project.id, "CLASSIFICATION_FAILED", details=exc.code,
                                         entity_type="email", entity_id=str(email.id))
        email.processing_status = MailStatus.processed
        return FeedbackResult("manual")
    has_attachments = session.execute(select(InputSource.id).where(
        InputSource.email_message_id == email.id,
        InputSource.source_type == SourceType.email_attachment)).first() is not None
    record = existing or FeedbackRecord(
        project_id=project.id, email_message_id=email.id,
        referenced_srs_version_id=project.current_srs_version_id,
        category=FeedbackCategory(output.category),
        secondary_categories=list(output.secondary_categories) or None,
        has_unprocessed_attachments=has_attachments, confidence=output.confidence,
        llm_output=output.model_dump(), status="pending")
    session.add(record)
    session.flush()
    event_log.log_event(session, project.id, "feedback_classified", ref_type="email",
                        ref_id=str(email.id), run_id=run_id,
                        details={"category": output.category, "confidence": output.confidence})
    email.processing_status = MailStatus.processed
    record.status = "processed"
    record.processed_at = _now()
    categories = {output.category, *output.secondary_categories}
    if output.confidence < settings.feedback_min_confidence or output.category in {"REJECTION", "UNKNOWN"}:
        issue_service.open_manual_review(session, project.id, "FEEDBACK_UNCERTAIN",
                                         details=f"{output.category} ({output.confidence:.2f})",
                                         entity_type="email", entity_id=str(email.id))
        return FeedbackResult("manual", record.id)
    if "APPROVAL" in categories:
        return FeedbackResult("approval", record.id)
    if categories & CHANGE_CATEGORIES and output.change_items:
        evidence_ids = _email_evidence(session, storage, settings, project.id, email)
        changed, unresolved = [], []
        for item in output.change_items:
            code = _apply_item(session, project.id, item, record, evidence_ids, run_id)
            if code:
                changed.append(code)
            elif item.action in ("modify", "remove", "confirm"):
                unresolved.append(item)
        if unresolved:
            _ask_which(session, settings, project, email, unresolved)
        if categories & QUESTION_CATEGORIES:
            draft_answer(session, settings, llm, project, email, listing)
        return FeedbackResult("changed" if changed else "question", record.id, tuple(dict.fromkeys(changed)))
    if categories & QUESTION_CATEGORIES:
        draft_answer(session, settings, llm, project, email, listing)
        return FeedbackResult("question", record.id)
    issue_service.open_manual_review(session, project.id, "FEEDBACK_UNCERTAIN", details=output.category,
                                     entity_type="email", entity_id=str(email.id))
    return FeedbackResult("manual", record.id)


def sender_contact_ids(session: Session, project_id: uuid.UUID, address: str | None) -> list:
    return [c.id for c, _ in project_service.contacts_for_project(session, project_id)
            if address and c.email.lower() == address.lower()]


__all__ = ["FeedbackResult", "analyze_email", "IssueType"]
