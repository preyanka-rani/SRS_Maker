"""SRS versions: list, diff summary, authorized download, regenerate, send."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project, require_roles
from app.core.config import get_settings
from app.core.envelope import ok
from app.core.errors import AppError, ConflictError, NotFoundError
from app.database.models import SrsVersion, UserRole, VersionStatus
from app.database.session import get_session
from app.integrations.storage.local import LocalStorage
from app.services import audit_service, srs_service
from app.services.llm_service import GuardedLlm
from app.workflows.runner import default_deps

router = APIRouter(prefix="/projects/{project_id}/srs", tags=["srs"])
_writers = require_roles(UserRole.admin, UserRole.analyst)
DOWNLOADABLE = (VersionStatus.ready, VersionStatus.sent, VersionStatus.approved, VersionStatus.final)
MIME = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf"}


class IntegrityError(AppError):
    status_code, code = 500, "integrity_error"


def _out(v: SrsVersion) -> dict:
    return {"id": str(v.id), "label": v.version_label, "kind": v.kind.value, "status": v.status.value,
            "change_summary": v.change_summary, "page_count": v.page_count,
            "substantive_hash": v.substantive_hash, "content_hash": v.content_hash,
            "qa": v.qa_report, "ready_at": v.ready_at.isoformat() if v.ready_at else None,
            "sent_at": v.sent_at.isoformat() if v.sent_at else None,
            "approved_at": v.approved_at.isoformat() if v.approved_at else None}


def _version(session: Session, project_id: uuid.UUID, label: str) -> SrsVersion:
    version = session.execute(select(SrsVersion).where(
        SrsVersion.project_id == project_id, SrsVersion.version_label == label,
        SrsVersion.status.notin_([VersionStatus.failed, VersionStatus.qa_failed]))).scalar_one_or_none()
    if version is None:
        raise NotFoundError("SRS version not found")
    return version


@router.get("/versions")
def list_versions(project_id: uuid.UUID, session: Session = Depends(get_session),
                  user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    rows = session.execute(select(SrsVersion).where(SrsVersion.project_id == project_id)
                           .order_by(SrsVersion.seq)).scalars()
    return ok([_out(v) for v in rows])


@router.get("/versions/{label}")
def get_version(project_id: uuid.UUID, label: str, session: Session = Depends(get_session),
                user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    return ok(_out(_version(session, project_id, label)))


@router.get("/versions/{label}/download")
def download(project_id: uuid.UUID, label: str, format: str = "docx",
             session: Session = Depends(get_session), user: CurrentUser = Depends(current_user)):
    get_member_project(session, user, project_id)
    if format not in MIME:
        raise NotFoundError("Unknown format")
    version = _version(session, project_id, label)
    if version.status not in DOWNLOADABLE:
        raise ConflictError("This version is not available for download")
    rel, expected = ((version.docx_path, version.docx_sha256) if format == "docx"
                     else (version.pdf_path, version.pdf_sha256))
    storage = LocalStorage(get_settings().storage_path)
    if not rel or storage.sha256(rel) != expected:
        raise IntegrityError("The stored file failed its integrity check")
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="srs_version",
                         entity_id=str(version.id), project_id=project_id, action="download",
                         details={"format": format})
    session.commit()
    name = Path(rel).name
    return FileResponse(storage.path(rel), media_type=MIME[format], filename=name,
                        headers={"Cache-Control": "no-store"})


@router.post("/generate", status_code=201)
def generate(project_id: uuid.UUID, session: Session = Depends(get_session),
             user: CurrentUser = Depends(_writers)) -> dict:
    """Generate (or reuse) the current draft now. Sending is a separate, explicit step."""
    project = get_member_project(session, user, project_id)
    deps = default_deps()
    llm = GuardedLlm(session, deps.llm_client(), project_id)
    version = srs_service.generate_draft(session, deps.storage, deps.settings, llm, project, None)
    if version is None:
        raise ConflictError("Nothing to generate yet, or QA failed (see manual reviews)")
    return ok(_out(version))


@router.post("/versions/{label}/send", status_code=202)
def send(project_id: uuid.UUID, label: str, session: Session = Depends(get_session),
         user: CurrentUser = Depends(_writers)) -> dict:
    project = get_member_project(session, user, project_id)
    version = _version(session, project_id, label)
    if version.status != VersionStatus.ready:
        raise ConflictError("Only a ready version can be sent")
    deps = default_deps()
    srs_service.queue_review_email(session, deps.storage, deps.settings, project, version)
    return ok({"queued": True})
