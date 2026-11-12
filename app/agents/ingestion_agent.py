"""Inbound mail: dedupe, bind to a project, store raw + sources, queue for the workflow.

Binding order (the subject alone is never enough):
  1. In-Reply-To/References hit one of OUR outbound Message-IDs and the sender is authorized;
  2. exactly one `[PRJ-XXXXXX]` token in the Subject AND the sender is an authorized contact;
  3. no token at all => a NEW request: mailbox intake opens a project for the sender.
A header/token that fails authorization is never auto-bound: manual review THREAD_BINDING_FAILED.
"""
import hashlib
import re
import uuid
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    EmailMailboxState,
    EmailMessage,
    EmailOutbox,
    EmailThread,
    InputSource,
    MailStatus,
    MatchMethod,
    Project,
    SourceType,
)
from app.domain.emailnorm import normalize_email
from app.domain.project_token import SUBJECT_TOKEN_RE
from app.integrations.email.auth_results import sender_verdict
from app.integrations.email.imap_adapter import FetchedMail, ImapAdapter
from app.integrations.email.mime_parser import ParsedEmail, parse_email
from app.integrations.storage.local import LocalStorage
from app.services import event_log, ingestion_service, issue_service, outbox_service, project_service

log = logging.getLogger(__name__)
_NOREPLY = re.compile(r"(^|[._-])(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?)([._-]|@)",
                      re.IGNORECASE)


def _is_automated(parsed: ParsedEmail, sender: str) -> bool:
    return parsed.is_auto_generated or parsed.is_bulk or bool(_NOREPLY.search(sender))


def _looks_like_request(settings: Settings, parsed: ParsedEmail) -> bool:
    """A new, unbound email only opens a project if it reads like a software/SRS request."""
    if any(a.data for a in parsed.attachments if not a.is_inline):
        return True
    haystack = f"{parsed.subject} {parsed.new_content_text or parsed.body_text}".lower()
    words = set(re.findall(r"[a-z]+", haystack))
    return any(k in words for k in settings.intake_keyword_list)


_X_OUTBOX = re.compile(r"X-SRS-Outbox-Id:\s*([0-9a-f-]{36})", re.IGNORECASE)


@dataclass(frozen=True)
class IngestOutcome:
    status: str  # new_request | reply | duplicate | ignored | unmatched | bounce
    project_id: uuid.UUID | None = None
    new_source_ids: tuple[uuid.UUID, ...] = ()
    uid: int | None = None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _authorized(session: Session, project_id: uuid.UUID, sender: str) -> bool:
    return any(normalize_email(c.email) == sender
               for c, _ in project_service.contacts_for_project(session, project_id))


def _bind_by_headers(session: Session, parsed: ParsedEmail) -> uuid.UUID | None:
    ids = ([parsed.in_reply_to] if parsed.in_reply_to else []) + list(parsed.references)
    if not ids:
        return None
    row = session.execute(select(EmailOutbox.project_id).where(
        EmailOutbox.message_id.in_(ids))).first()
    return row[0] if row else None


def _store_message(session: Session, storage: LocalStorage, settings: Settings, parsed: ParsedEmail,
                   raw: bytes, uid: int | None, uidvalidity: int | None,
                   project_id: uuid.UUID | None, method: MatchMethod, status: MailStatus,
                   failure: str | None = None) -> EmailMessage:
    verdict, untrusted = sender_verdict(parsed.authentication_results, settings.trusted_authserv_set)
    sender = parsed.from_addresses[0] if parsed.from_addresses else None
    msg = EmailMessage(
        project_id=project_id, direction="inbound", message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to, references=list(parsed.references) or None,
        from_address=sender, from_count=len(parsed.from_addresses), sender_address=sender,
        reply_to_addresses=list(parsed.reply_to) or None, to_addresses=list(parsed.to) or None,
        cc_addresses=list(parsed.cc) or None, subject=parsed.subject, body_text=parsed.body_text,
        new_content_text=parsed.new_content_text,
        raw_path=storage.save_bytes("mail_raw", project_id or "unmatched", raw, ".eml"),
        raw_sha256=hashlib.sha256(raw).hexdigest(), imap_uid=uid, imap_uidvalidity=uidvalidity,
        received_at=parsed.date or _now(), auth_results_untrusted_count=untrusted,
        sender_auth_verdict=verdict, is_auto_generated=parsed.is_auto_generated,
        is_dsn=parsed.is_dsn, match_method=method, match_failure_reason=failure,
        processing_status=status)
    session.add(msg)
    session.flush()
    return msg


def _own_address(settings: Settings) -> str | None:
    return normalize_email(settings.mail_from_address) if settings.mail_from_address else None


def _bind(session: Session, parsed: ParsedEmail, sender: str
          ) -> tuple[uuid.UUID | None, MatchMethod, str | None]:
    by_header = _bind_by_headers(session, parsed)
    if by_header:
        if _authorized(session, by_header, sender):
            return by_header, MatchMethod.headers, None
        return by_header, MatchMethod.unmatched, "sender_not_authorized_for_thread"
    tokens = SUBJECT_TOKEN_RE.findall(parsed.subject or "")
    if len(set(tokens)) > 1 or len(tokens) > 1:
        return None, MatchMethod.unmatched, "multiple_project_tokens"
    if tokens:
        project = session.execute(
            select(Project).where(Project.project_token == tokens[0])).scalar_one_or_none()
        if project is None:  # a token we never issued (e.g. copied text): treat as a new request
            return None, MatchMethod.manual, None
        if not _authorized(session, project.id, sender):
            return project.id, MatchMethod.unmatched, "sender_not_authorized_for_token"
        return project.id, MatchMethod.subject_token_sender, None
    return None, MatchMethod.manual, None


def _thread(session: Session, msg: EmailMessage, parsed: ParsedEmail) -> None:
    root = (parsed.references[0] if parsed.references else parsed.in_reply_to) or parsed.message_id
    thread = session.execute(
        select(EmailThread).where(EmailThread.root_message_id == root)).scalar_one_or_none()
    if thread is None:
        thread = EmailThread(project_id=msg.project_id, root_message_id=root,
                             subject_normalized=project_service.clean_subject(parsed.subject))
        session.add(thread)
        session.flush()
    msg.thread_id = thread.id


def _register_attachments(session, storage, settings, project_id, parsed: ParsedEmail,
                          email_id: uuid.UUID | None) -> list[InputSource]:
    out: list[InputSource] = []
    for att in parsed.attachments:
        if not att.data:
            continue
        res = ingestion_service.register_source(
            session, storage, settings, project_id, att.data, att.filename,
            SourceType.email_attachment, mime=att.content_type, is_inline=att.is_inline,
            email_message_id=email_id)
        if res.created:
            out.append(res.source)
    return out


def _register_all(session, storage, settings, project_id, parsed: ParsedEmail) -> list[InputSource]:
    """New request: the body and every attachment become ordinary sources of the project."""
    sources: list[InputSource] = []
    body = (parsed.body_text or "").strip()
    if body:
        text = f"Subject: {parsed.subject}\n\n{body}"
        res = ingestion_service.register_source(
            session, storage, settings, project_id, text.encode("utf-8"), "email-body.txt",
            SourceType.email_body, mime="text/plain")
        if res.created:
            sources.append(res.source)
    sources.extend(_register_attachments(session, storage, settings, project_id, parsed, None))
    return sources


def _ack_new_request(session, settings, project, msg, parsed, sender, n_sources: int) -> None:
    extra = f" and {n_sources - 1} attachment(s)" if n_sources > 1 else ""
    body = (
        f'Hello,\n\nThank you for your request. We opened project "{project.title}" '
        f"(reference {project.project_token}) and received your message{extra}.\n\n"
        "We are analysing it now and will email you a draft Software Requirements Specification "
        "(SRS) shortly. When you reply, please keep the subject line unchanged so that we can "
        f"match your reply to this project.\n\nRegards,\n{settings.smtp_from_name}")
    outbox_service.enqueue(
        session, settings, project, kind="attachment_notice", to=[sender],
        subject=outbox_service.subject_for(project, "request received"), body_text=body,
        idempotency_key=f"ack:{msg.id}", in_reply_to=parsed.message_id,
        references=[*parsed.references, parsed.message_id])


def _handle_dsn(session, storage, settings, parsed, raw, uid, uidvalidity) -> IngestOutcome:
    _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, None,
                   MatchMethod.unmatched, MailStatus.ignored, "dsn")
    match = _X_OUTBOX.search(raw.decode("utf-8", errors="ignore"))
    if match:
        outbox_service.mark_bounced(session, match.group(1), "Delivery failure notification received")
    return IngestOutcome("bounce")


def ingest_raw_email(session: Session, storage: LocalStorage, settings: Settings, raw: bytes,
                     uid: int | None = None, uidvalidity: int | None = None) -> IngestOutcome:
    parsed = parse_email(raw)
    if session.execute(select(EmailMessage.id).where(
            EmailMessage.message_id == parsed.message_id)).first():
        return IngestOutcome("duplicate")
    if parsed.is_dsn:
        return _handle_dsn(session, storage, settings, parsed, raw, uid, uidvalidity)
    sender = parsed.from_addresses[0] if len(parsed.from_addresses) == 1 else None
    if sender is None or sender == _own_address(settings) or _is_automated(parsed, sender):
        _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, None,
                       MatchMethod.unmatched, MailStatus.ignored, "auto_or_self_or_no_sender")
        return IngestOutcome("ignored")

    project_id, method, failure = _bind(session, parsed, sender)
    if failure:
        msg = _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, project_id,
                             MatchMethod.unmatched, MailStatus.unmatched, failure)
        if project_id:
            issue_service.open_manual_review(session, project_id, "THREAD_BINDING_FAILED",
                                             details=failure, entity_type="email",
                                             entity_id=str(msg.id))
            event_log.log_event(session, project_id, "thread_binding_failed", ref_type="email",
                                ref_id=str(msg.id), details={"reason": failure})
        return IngestOutcome("unmatched", project_id)

    if project_id is None:  # a brand-new request
        if not _looks_like_request(settings, parsed):
            _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, None,
                           MatchMethod.unmatched, MailStatus.ignored, "not_srs_related")
            return IngestOutcome("ignored")
        if not settings.auto_create_project_from_email:
            _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, None,
                           MatchMethod.unmatched, MailStatus.unmatched, "no_project")
            return IngestOutcome("unmatched")
        contact = project_service.find_or_create_contact(session, sender, None)
        project = project_service.create_project_for_contact(session, settings, contact,
                                                             parsed.subject)
        msg = _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, project.id,
                             MatchMethod.manual, MailStatus.processed)
        sources = _register_all(session, storage, settings, project.id, parsed)
        _ack_new_request(session, settings, project, msg, parsed, sender, len(sources))
        event_log.log_event(session, project.id, "email_received", ref_type="email",
                            ref_id=str(msg.id), details={"kind": "new_request"})
        return IngestOutcome("new_request", project.id, tuple(s.id for s in sources))

    msg = _store_message(session, storage, settings, parsed, raw, uid, uidvalidity, project_id,
                         method, MailStatus.queued)
    _thread(session, msg, parsed)
    sources = _register_attachments(session, storage, settings, project_id, parsed, msg.id)
    event_log.log_event(session, project_id, "email_received", ref_type="email", ref_id=str(msg.id),
                        details={"kind": "reply", "attachments": len(sources)})
    return IngestOutcome("reply", project_id, tuple(s.id for s in sources))


def poll_mailbox(session_factory, storage: LocalStorage, settings: Settings,
                 adapter: ImapAdapter | None = None) -> list[IngestOutcome]:
    """Fetch new messages in UID order; each commit is the acknowledgement of that message."""
    adapter = adapter or ImapAdapter(settings)
    key = f"{settings.imap_username}/{settings.imap_folder}"
    with session_factory() as s:
        state = s.get(EmailMailboxState, key)
        last_uid = state.last_uid if state else 0
    mails: list[FetchedMail] = adapter.fetch_after(last_uid)
    outcomes: list[IngestOutcome] = []
    for mail in sorted(mails, key=lambda m: m.uid):
        with session_factory() as s:
            state = s.get(EmailMailboxState, key)
            if state is None:
                state = EmailMailboxState(mailbox=key, last_uid=0)
                s.add(state)
            state.uidvalidity = mail.uidvalidity
            try:
                with s.begin_nested():
                    outcome = ingest_raw_email(s, storage, settings, mail.raw, mail.uid,
                                               mail.uidvalidity)
            except Exception as exc:  # noqa: BLE001 - one bad message must not block the mailbox
                state.last_error = f"uid {mail.uid}: {type(exc).__name__}: {exc}"[:500]
                outcome = IngestOutcome("ignored")
            state.last_uid = max(state.last_uid, mail.uid)
            state.last_polled_at = _now()
        outcomes.append(replace(outcome, uid=mail.uid))
    _mark_read(adapter, settings, outcomes)
    return outcomes


def _mark_read(adapter, settings: Settings, outcomes: list[IngestOutcome]) -> None:
    """Mails that start or continue a project are marked read; promos and the like stay unread."""
    uids = [o.uid for o in outcomes if o.uid and o.status in ("new_request", "reply")]
    if not (uids and settings.imap_mark_seen):
        return
    try:
        adapter.mark_seen(uids)
    except Exception:  # noqa: BLE001 - never lose processed mail over a flag update
        log.warning("could not mark %d message(s) as read", len(uids), exc_info=True)
