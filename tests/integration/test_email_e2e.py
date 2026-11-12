"""End-to-end: client email -> project -> SRS draft emailed -> client change -> revised SRS ->
client approval -> verified final v1.0 emailed. Fake IMAP/SMTP/LLM; real Postgres, docxtpl, PDF."""
import zipfile
from email.message import EmailMessage

import pytest
from sqlalchemy import select, text

from app.core.config import Settings
from app.database.models import (
    Approval,
    EmailOutbox,
    OutboxStatus,
    Project,
    ProjectStatus,
    Requirement,
    SrsVersion,
    VersionStatus,
)
from app.database.session import get_engine, session_scope
from app.integrations.email.imap_adapter import FetchedMail
from app.integrations.storage.local import LocalStorage
from app.llm.client import FakeLlmClient
from app.llm.output_schemas import (
    ApprovalSignal,
    ChangeItemDraft,
    ExtractionOutput,
    FeedbackOutput,
    RequirementCandidateDraft,
    SrsNarrativeOutput,
)
from app.services import email_service, ingestion_service, outbox_service
from app.workflows import runner
from app.workflows.deps import WorkflowDeps

CLIENT = "client@acme-client.com"
SYSTEM = "srs.system@example.test"


class FakeImap:
    def __init__(self):
        self.mails: list[FetchedMail] = []
        self._uid = 0
        self.seen: list[int] = []

    def mark_seen(self, uids: list[int]) -> None:
        self.seen.extend(uids)

    def add(self, msg: EmailMessage) -> None:
        self._uid += 1
        self.mails.append(FetchedMail(self._uid, 1, msg.as_bytes()))

    def fetch_after(self, last_uid: int, limit: int = 50) -> list[FetchedMail]:
        return [m for m in self.mails if m.uid > last_uid]


class FakeSmtp:
    def __init__(self):
        self.sent = []

    def send(self, mail) -> int:
        self.sent.append(mail)
        return 250


def mail(subject: str, body: str, msg_id: str, *, headers: dict | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = f"Client <{CLIENT}>", SYSTEM, subject, msg_id
    m["Authentication-Results"] = "mx.google.com; dkim=pass; spf=pass; dmarc=pass"
    for key, value in (headers or {}).items():
        m[key] = value
    m.set_content(body)
    return m


def draft(statement: str, title: str) -> RequirementCandidateDraft:
    return RequirementCandidateDraft(
        req_type="FR", title=title, statement=statement, evidence_refs=["E1"], confidence=0.9,
        actors="Finance user", acceptance_criteria=[{"given": "an invoice", "when": "exported",
                                                     "then": "a file is produced"}])


NARRATIVE = SrsNarrativeOutput(executive_summary="A portal for invoices.", purpose="Record the requirements.",
                               scope="Invoice handling.", business_context="Finance team workflow.")


@pytest.fixture
def system(app_db, tmp_path):
    settings = Settings(_env_file=None, smtp_username=SYSTEM, imap_username=SYSTEM,
                        smtp_password="x", imap_password="x", smtp_host="h", imap_host="h",
                        storage_root=str(tmp_path / "store"))
    storage = LocalStorage(tmp_path / "store")
    fake = FakeLlmClient()
    deps = WorkflowDeps(settings, storage, session_scope, lambda: fake)
    yield deps, fake, FakeImap(), FakeSmtp()
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE checkpoint_writes, checkpoint_blobs, checkpoints CASCADE"))


def poll(deps, imap):
    outcomes = email_service.poll_mailbox(session_scope, deps.storage, deps.settings, imap)
    for outcome in outcomes:
        for source_id in outcome.new_source_ids:
            with session_scope() as s:
                ingestion_service.ingest_text_source(s, deps.storage, deps.settings, source_id)
    return outcomes


def flush_outbox(deps, smtp):
    return outbox_service.dispatch_due(session_scope, deps.storage, deps.settings, smtp)


def outbox_rows(pid):
    with session_scope() as s:
        return [(o.kind, o.status.value, o.subject, o.message_id, o.attachments)
                for o in s.execute(select(EmailOutbox).where(EmailOutbox.project_id == pid)
                                   .order_by(EmailOutbox.created_at)).scalars()]


def test_full_email_lifecycle(system):
    deps, llm, imap, smtp = system

    # 1) a stranger emails the system mailbox
    imap.add(mail("Invoice portal request", "We need a portal where finance can export invoices as PDF.",
                  "<c1@client>"))
    outcomes = poll(deps, imap)
    assert outcomes[0].status == "new_request"
    assert imap.seen == [1]  # the mail we answer is marked as read
    pid = outcomes[0].project_id
    llm.enqueue(ExtractionOutput, ExtractionOutput(requirements=[
        draft("The system shall export invoices as PDF.", "Invoice export")]))
    llm.enqueue(SrsNarrativeOutput, NARRATIVE)
    assert runner.run_for_project(pid, "t1", deps) == "processed"

    with session_scope() as s:
        v1 = s.execute(select(SrsVersion).where(SrsVersion.project_id == pid)).scalar_one()
        assert (v1.version_label, v1.status.value) == ("0.1", "ready")
        assert v1.qa_report["passed"] is True and v1.page_count > 0
        project = s.get(Project, pid)
        token = project.project_token
        assert project.status == ProjectStatus.in_review
    kinds = [r[0] for r in outbox_rows(pid)]
    assert kinds == ["attachment_notice", "review"]  # acknowledgement + SRS draft
    assert flush_outbox(deps, smtp)["sent"] == 2
    review = next(m for m in smtp.sent if "for review" in m.subject)
    assert f"[{token}]" in review.subject and len(review.attachments) == 2
    assert "answer these questions" in review.body_text  # thin input => follow-up questions
    docx_name, _, docx_bytes_ = review.attachments[0]
    path = deps.storage.root / "x.docx"
    path.write_bytes(docx_bytes_)
    with zipfile.ZipFile(path) as z:  # a real, tag-free Word document
        xml = z.read("word/document.xml").decode()
    assert "FR-001" in xml and "{{" not in xml and "DLAS" not in xml
    with session_scope() as s:
        assert s.execute(select(SrsVersion.status)).scalar_one() == VersionStatus.sent

    # 2) the client replies (headers intact) asking for a change -> v0.2
    imap.add(mail(f"Re: [{token}] Invoice portal – SRS v0.1 for review",
                  "Please also let finance export invoices as CSV.", "<c2@client>",
                  headers={"In-Reply-To": review.message_id, "References": review.message_id}))
    assert poll(deps, imap)[0].status == "reply"
    llm.enqueue(FeedbackOutput, FeedbackOutput(
        category="NEW_REQUIREMENT", confidence=0.95,
        change_items=[ChangeItemDraft(action="add", req_type="FR", title="CSV export",
                                      statement="The system shall export invoices as CSV.",
                                      evidence_excerpt="export invoices as CSV")]))
    llm.enqueue(SrsNarrativeOutput, NARRATIVE)
    assert runner.run_for_project(pid, "t2", deps) == "processed"
    with session_scope() as s:
        labels = [(v.version_label, v.status.value) for v in s.execute(
            select(SrsVersion).where(SrsVersion.project_id == pid).order_by(SrsVersion.seq)).scalars()]
        codes = [r.code for r in s.execute(select(Requirement).where(Requirement.project_id == pid)
                                            .order_by(Requirement.code)).scalars()]
    assert labels == [("0.1", "superseded"), ("0.2", "ready")] and codes == ["FR-001", "FR-002"]
    assert flush_outbox(deps, smtp)["sent"] == 1
    review2 = smtp.sent[-1]
    assert "v0.2" in review2.subject

    # 3) the client approves version 0.2 -> verified final v1.0
    imap.add(mail(f"Re: [{token}] Invoice portal – SRS v0.2 for review",
                  "I approve SRS version 0.2 with no further changes.", "<c3@client>",
                  headers={"In-Reply-To": review2.message_id, "References": review2.message_id}))
    assert poll(deps, imap)[0].status == "reply"
    llm.enqueue(FeedbackOutput, FeedbackOutput(category="APPROVAL", confidence=0.97))
    llm.enqueue(ApprovalSignal, ApprovalSignal(
        explicit_approval=True, approval_scope_all_requirements=True, has_change_requests=False,
        has_questions=False, states_version="0.2", quoted_phrase="I approve SRS version 0.2",
        confidence=0.97))
    assert runner.run_for_project(pid, "t3", deps) == "processed"
    with session_scope() as s:
        approval = s.execute(select(Approval)).scalar_one()
        assert approval.outcome == "accepted" and all(approval.checks["checks"].values())
        final = s.execute(select(SrsVersion).where(SrsVersion.project_id == pid,
                                                   SrsVersion.version_label == "1.0")).scalar_one()
        approved = s.execute(select(SrsVersion).where(SrsVersion.version_label == "0.2")).scalar_one()
        assert final.status == VersionStatus.final and approved.status == VersionStatus.approved
        assert final.substantive_hash == approved.substantive_hash  # content identical
        assert s.get(Project, pid).status == ProjectStatus.finalized
        assert {r.status.value for r in s.execute(select(Requirement)).scalars()} == {"confirmed"}
    assert flush_outbox(deps, smtp)["sent"] == 1
    assert "FINAL" in smtp.sent[-1].subject
    with session_scope() as s:
        assert all(o.status == OutboxStatus.sent for o in s.execute(select(EmailOutbox)).scalars())


def test_unauthorised_token_reply_is_not_bound(system):
    deps, llm, imap, smtp = system
    imap.add(mail("Portal", "We need a portal for invoices.", "<a1@client>"))
    pid = poll(deps, imap)[0].project_id
    with session_scope() as s:
        token = s.get(Project, pid).project_token
    stranger = mail(f"Re: [{token}] Portal", "I approve everything.", "<evil@x>")
    del stranger["From"]
    stranger["From"] = "Mallory <mallory@evil.test>"
    imap.add(stranger)
    outcome = poll(deps, imap)[0]
    assert outcome.status == "unmatched"
    with session_scope() as s:
        from app.database.models import ManualReview

        reviews = s.execute(select(ManualReview.reason_code)).scalars().all()
    assert "THREAD_BINDING_FAILED" in reviews
