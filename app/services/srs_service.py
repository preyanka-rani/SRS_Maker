"""Generate, freeze and send SRS versions; build the final v1.0 snapshot.

A draft is generated only when the input fingerprint changed. The finished version is frozen by
database triggers; the final v1.0 is rendered from the approved version's STORED context (no
LLM call) with an allowlist of presentation-only differences, then verified.
"""
import copy
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import elicitation_agent, srs_agent
from app.core.config import Settings
from app.database.models import (
    Project,
    ProjectStatus,
    SrsSection,
    SrsVersion,
    VersionKind,
    VersionStatus,
)
from app.integrations.storage.local import LocalStorage
from app.services import event_log, issue_service, outbox_service, project_service
from app.services.llm_service import GuardedLlm
from app.srs_engine import version_manager as vm
from app.srs_engine.context_builder import SrsContext, build_context
from app.srs_engine.docx_generator import RENDERER_VERSION, ensure_template, render_docx, template_sha256
from app.srs_engine.pdf_generator import export_pdf
from app.srs_engine.qa import run_qa

REVIEW_KINDS = ("review", "revised_review")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _approver_names(session: Session, project_id: uuid.UUID) -> str:
    names = [c.full_name for c, pc in project_service.contacts_for_project(session, project_id)
             if pc.can_approve]
    return ", ".join(names)


def _revisions(session: Session, project_id: uuid.UUID, label: str, summary: str) -> list[dict]:
    rows = [{"version": v.version_label, "date": (v.ready_at or v.created_at).strftime("%Y-%m-%d"),
             "author": "SRS Generation System", "description": v.change_summary or "Draft"}
            for v in session.execute(select(SrsVersion).where(
                SrsVersion.project_id == project_id, SrsVersion.kind == VersionKind.draft,
                SrsVersion.status.in_([VersionStatus.ready, VersionStatus.sent, VersionStatus.superseded,
                                       VersionStatus.approved])).order_by(SrsVersion.seq)).scalars()]
    rows.append({"version": label, "date": _now().strftime("%Y-%m-%d"),
                 "author": "SRS Generation System", "description": summary})
    return rows


def _paths(project_id: uuid.UUID, version: SrsVersion, project: Project) -> tuple[str, str]:
    base = f"generated/{project_id}/{version.id}/attempt-{version.generation_attempt}"
    stem = f"SRS-{project.code}-v{version.version_label}"
    return f"{base}/{stem}.docx", f"{base}/{stem}.pdf"


def _store_sections(session: Session, version: SrsVersion, context: SrsContext, method: str) -> None:
    session.add(SrsSection(project_id=version.project_id, srs_version_id=version.id,
                           section_key="narrative", content=context.data["narrative"],
                           generation_method=method))
    session.add(SrsSection(project_id=version.project_id, srs_version_id=version.id,
                           section_key="context", content=context.data, generation_method="D"))
    session.flush()


def _render_and_qa(session: Session, storage: LocalStorage, settings: Settings, project: Project,
                   version: SrsVersion, context: SrsContext) -> tuple[bool, dict]:
    docx_rel, pdf_rel = _paths(project.id, version, project)
    docx_path, pdf_path = storage.path(docx_rel), storage.path(pdf_rel)
    version.docx_sha256 = render_docx(settings, context, docx_path)
    version.pdf_sha256, version.page_count = export_pdf(settings, docx_path, context, pdf_path)
    version.docx_path, version.pdf_path = docx_rel, pdf_rel
    qa = run_qa(docx_path, version.page_count, context, settings.leakage_denylist_path)
    version.qa_report = qa.as_report()
    version.content_hash = vm.sha256_json(context.data)
    return qa.passed, version.qa_report


def generate_draft(session: Session, storage: LocalStorage, settings: Settings, llm: GuardedLlm,
                   project: Project, run_id: uuid.UUID | None) -> SrsVersion | None:
    """Returns the new draft, the unchanged current version, or None when nothing can be built."""
    snaps = vm.live_snapshots(session, project.id)
    if not snaps:
        return None
    elicitation_agent.update_questions(session, llm, project, snaps)  # before hashing: issues count
    prompt_versions = {srs_agent.PROMPT_ID: srs_agent.prompt().version,
                       elicitation_agent.PROMPT_ID: elicitation_agent.prompt().version}
    template_version = settings.template_version
    hashes = vm.compute_hashes(session, project, template_version, prompt_versions)
    current = vm.latest_live(session, project.id)
    if current is not None and current.input_fingerprint == hashes["input_fingerprint"]:
        return current  # nothing changed: no new version
    vm.abandon_stale_generating(session, project.id)
    label = vm.next_draft_label(session, project.id)
    summary = vm.change_summary(session, current, snaps)
    ensure_template(settings)
    version = SrsVersion(
        project_id=project.id, seq=vm.next_seq(session, project.id), version_label=label,
        kind=VersionKind.draft, status=VersionStatus.generating,
        base_version_id=current.id if current else None, template_version=template_version,
        prompt_versions=prompt_versions, renderer_version=RENDERER_VERSION,
        created_by_run_id=run_id, change_summary=summary, **hashes)
    session.add(version)
    session.flush()
    narrative, method = srs_agent.write_narrative(llm, project, snaps)
    context = build_context(session, project, label=label, status="DRAFT FOR CLIENT REVIEW",
                            narrative=narrative, prepared_by=settings.srs_prepared_by,
                            revisions=_revisions(session, project.id, label, summary),
                            approver_names=_approver_names(session, project.id))
    passed, report = _render_and_qa(session, storage, settings, project, version, context)
    if not passed:
        version.status, version.error = VersionStatus.qa_failed, "; ".join(report["problems"])[:1000]
        issue_service.open_manual_review(session, project.id, "QA_FAILED",
                                         details=version.error, entity_type="srs_version",
                                         entity_id=str(version.id), workflow_run_id=run_id)
        event_log.log_event(session, project.id, "srs_qa_failed", ref_type="srs_version",
                            ref_id=str(version.id), run_id=run_id, details=report)
        session.flush()
        return None
    vm.pin_requirements(session, version, snaps)
    _store_sections(session, version, context, method)
    version.status, version.ready_at = VersionStatus.ready, _now()
    session.flush()
    for rel in (version.docx_path, version.pdf_path):
        storage.make_readonly(rel)
    vm.supersede_older(session, project.id, version.id)
    project.current_srs_version_id = version.id
    project.status = ProjectStatus.in_review
    project.template_version = template_version
    event_log.log_event(session, project.id, "version_ready", ref_type="srs_version",
                        ref_id=str(version.id), run_id=run_id,
                        details={"label": label, "template_sha256": template_sha256(ensure_template(settings))})
    return version


def _attachments(storage: LocalStorage, version: SrsVersion) -> list[dict]:
    return [{"filename": storage.path(rel).name, "mime": mime, "path": rel, "sha256": sha}
            for rel, mime, sha in (
                (version.docx_path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 version.docx_sha256),
                (version.pdf_path, "application/pdf", version.pdf_sha256))]


def _recipients(session: Session, project: Project) -> list[str]:
    return [c.email for c, pc in project_service.contacts_for_project(session, project.id)]


def queue_review_email(session: Session, storage: LocalStorage, settings: Settings,
                       project: Project, version: SrsVersion, *, in_reply_to: str | None = None,
                       references: list[str] | None = None) -> None:
    recipients = _recipients(session, project)
    if not recipients:
        issue_service.open_manual_review(session, project.id, "NO_RECIPIENT",
                                         entity_type="project", entity_id=str(project.id))
        return
    questions = vm.blocking_questions(session, project.id)
    q_block = ("\nOpen questions we need your help with:\n" + "\n".join(f"  - {q}" for q in questions)
               + "\n") if questions else ""
    label = version.version_label
    body = (
        f"Hello,\n\nPlease find attached the Software Requirements Specification (SRS) for "
        f'"{project.title}", version {label}, in Word and PDF format.\n\n'
        f"{version.change_summary}\n{q_block}\n"
        "Please review it and reply to this email (keeping the subject line unchanged) with:\n"
        "  - any requirement to add, change or remove, or any question; or\n"
        f"  - if everything is correct and complete: \"I approve SRS version {label} with no "
        "further changes.\"\n\n"
        f"Reference: {project.project_token} | Version {label} | Document id {version.content_hash[:12]}\n\n"
        f"Regards,\n{settings.smtp_from_name}")
    kind = "review" if version.base_version_id is None else "revised_review"
    outbox_service.enqueue(
        session, settings, project, kind=kind, to=recipients,
        subject=outbox_service.subject_for(project, f"SRS v{label} for review"), body_text=body,
        idempotency_key=f"{kind}:{version.id}", attachments=_attachments(storage, version),
        srs_version_id=version.id, in_reply_to=in_reply_to, references=references)


def mark_version_sent(session: Session, srs_version_id: uuid.UUID) -> None:
    version = session.get(SrsVersion, srs_version_id)
    if version is None or version.status != VersionStatus.ready:
        return
    version.status, version.sent_at = VersionStatus.sent, _now()
    project = session.get(Project, version.project_id)
    if project.status in (ProjectStatus.in_review, ProjectStatus.revising, ProjectStatus.extracting,
                          ProjectStatus.draft):
        project.status = ProjectStatus.awaiting_client


# ---- final snapshot ---------------------------------------------------------------------------

def _stored_context(session: Session, version_id: uuid.UUID) -> dict:
    section = session.execute(select(SrsSection).where(
        SrsSection.srs_version_id == version_id, SrsSection.section_key == "context")).scalar_one()
    return copy.deepcopy(section.content)


def build_final_context(session: Session, project: Project, approved: SrsVersion,
                        approval_text: str, approver: str) -> SrsContext:
    """Allowlist of differences from the approved version: version label, status, approval line,
    one appended revision row and CONFIRMED status labels. Nothing else may change."""
    data = _stored_context(session, approved.id)
    data["doc"]["version"] = "1.0"
    data["doc"]["status"] = "FINAL — APPROVED"
    data["final_approval"] = {"text": approval_text}
    data["revisions"] = data["revisions"] + [{
        "version": "1.0", "date": _now().strftime("%Y-%m-%d"), "author": approver,
        "description": f"Approved by the client. Content identical to draft v{approved.version_label}."}]
    for key in ("frs", "nfrs", "secs", "rules", "ints", "others"):
        for row in data[key]:
            if row["status_label"] != "NEEDS CLARIFICATION":
                row["status_label"] = "CONFIRMED"
    return SrsContext(data)


def finalize(session: Session, storage: LocalStorage, settings: Settings, project: Project,
             approved: SrsVersion, approval_text: str, approver: str,
             run_id: uuid.UUID | None) -> SrsVersion | None:
    """Render v1.0 from the approved snapshot and verify it. Returns None on a failed check."""
    context = build_final_context(session, project, approved, approval_text, approver)
    version = SrsVersion(
        project_id=project.id, seq=vm.next_seq(session, project.id), version_label="1.0",
        kind=VersionKind.final, status=VersionStatus.generating, base_version_id=approved.id,
        template_version=approved.template_version, prompt_versions=approved.prompt_versions,
        renderer_version=RENDERER_VERSION, created_by_run_id=run_id,
        change_summary="Final approved version.", input_fingerprint=approved.input_fingerprint,
        requirement_set_hash=approved.requirement_set_hash,
        substantive_hash=approved.substantive_hash)
    session.add(version)
    session.flush()
    passed, report = _render_and_qa(session, storage, settings, project, version, context)
    same_pins = vm.pinned_revisions(session, approved.id)
    live = {s["code"]: s["content_revision"] for s in vm.live_snapshots(session, project.id)}
    fresh = vm.compute_hashes(session, project, approved.template_version or settings.template_version,
                              approved.prompt_versions or {})
    verified = passed and fresh["substantive_hash"] == approved.substantive_hash \
        and len(same_pins) == len(live)
    if not verified:
        version.status = VersionStatus.failed
        version.error = "final snapshot verification failed"
        issue_service.open_manual_review(session, project.id, "FINAL_SNAPSHOT_MISMATCH",
                                         details=str(report.get("problems")),
                                         entity_type="srs_version", entity_id=str(approved.id),
                                         workflow_run_id=run_id)
        session.flush()
        return None
    vm.pin_requirements(session, version, vm.live_snapshots(session, project.id))
    _store_sections(session, version, context, "D")
    version.status, version.finalized_at = VersionStatus.final, _now()
    session.flush()
    for rel in (version.docx_path, version.pdf_path):
        storage.make_readonly(rel)
    project.current_srs_version_id = version.id
    project.status = ProjectStatus.finalized
    return version


def queue_final_email(session: Session, storage: LocalStorage, settings: Settings, project: Project,
                      version: SrsVersion) -> None:
    recipients = _recipients(session, project)
    if not recipients:
        return
    body = (f'Hello,\n\nThank you for your approval. The final Software Requirements Specification '
            f'(version 1.0) for "{project.title}" is attached in Word and PDF format.\n\n'
            f"Reference: {project.project_token}\n\nRegards,\n{settings.smtp_from_name}")
    outbox_service.enqueue(
        session, settings, project, kind="final", to=recipients,
        subject=outbox_service.subject_for(project, "SRS v1.0 FINAL"), body_text=body,
        idempotency_key=f"final:{version.id}", attachments=_attachments(storage, version),
        srs_version_id=version.id)
