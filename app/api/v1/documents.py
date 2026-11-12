"""Document upload and source/evidence/transcript read endpoints."""
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project, require_roles
from app.core.config import get_settings
from app.core.envelope import fail, ok
from app.core.errors import ConflictError, NotFoundError
from app.database.models import (
    ExtractionStatus,
    InputSource,
    ProcessingClass,
    SourceEvidence,
    SourceType,
    UserRole,
)
from app.database.session import get_session
from app.integrations.storage.local import LocalStorage
from app.processors.attachment_classifier import AttachmentInfo, classify
from app.schemas.documents import EvidenceOut, InputSourceOut
from app.services import audit_service, ingestion_service
from app.workers.tasks import dispatch_source

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])
_writers = require_roles(UserRole.admin, UserRole.analyst)
_UPLOADABLE = {ProcessingClass.text_supported, ProcessingClass.audio}
_CHUNK = 1024 * 1024


async def _read_limited(upload: UploadFile, limit_bytes: int) -> bytes | None:
    """Read at most limit_bytes; None means the limit was exceeded (never buffer unbounded)."""
    chunks, total = [], 0
    while block := await upload.read(_CHUNK):
        total += len(block)
        if total > limit_bytes:
            return None
        chunks.append(block)
    return b"".join(chunks)


@router.post("", status_code=202)
async def upload_document(
    project_id: uuid.UUID,
    file: UploadFile = File(...),
    note: str | None = Form(default=None),
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
):
    project = get_member_project(session, user, project_id)
    settings = get_settings()
    cls = classify(AttachmentInfo(file.filename, file.content_type))
    if cls not in _UPLOADABLE:
        return JSONResponse(status_code=415, content=fail(
            "unsupported_media_type", "Upload PDF, DOCX, TXT or an audio file"))
    # audio can legitimately be larger than a document; the pipeline enforces duration limits
    limit_mb = max(settings.max_upload_mb, 100) if cls == ProcessingClass.audio else settings.max_upload_mb
    data = await _read_limited(file, limit_mb * 1024 * 1024)
    if data is None:
        return JSONResponse(status_code=413, content=fail("too_large", f"File exceeds {limit_mb} MB"))
    if not data:
        return JSONResponse(status_code=422, content=fail("empty_file", "File is empty"))
    storage = LocalStorage(settings.storage_path)
    result = ingestion_service.register_source(
        session, storage, settings, project.id, data, file.filename, SourceType.upload,
        mime=file.content_type, uploaded_by=user.id)
    if not result.created:
        raise ConflictError("This file was already uploaded",
                            {"existing_id": str(result.source.id)})
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="input_source",
                         entity_id=str(result.source.id), project_id=project.id, action="upload",
                         details={"filename": file.filename, "sha256": result.source.sha256,
                                  "note": note})
    session.commit()  # the task must see the committed row
    dispatch_source(result.source)
    return ok(InputSourceOut.model_validate(result.source))


@router.get("")
def list_documents(project_id: uuid.UUID, status: str | None = None, cls: str | None = None,
                   source_type: str | None = None, session: Session = Depends(get_session),
                   user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    query = select(InputSource).where(InputSource.project_id == project_id)
    if status:
        query = query.where(InputSource.extraction_status == status)
    if cls:
        query = query.where(InputSource.processing_class == cls)
    if source_type:
        query = query.where(InputSource.source_type == source_type)
    rows = session.execute(query.order_by(InputSource.created_at)).scalars().all()
    return ok([InputSourceOut.model_validate(r) for r in rows])


def _get_source(session: Session, project_id: uuid.UUID, doc_id: uuid.UUID) -> InputSource:
    source = session.execute(select(InputSource).where(
        InputSource.project_id == project_id, InputSource.id == doc_id)).scalar_one_or_none()
    if source is None:
        raise NotFoundError("Document not found")
    return source


@router.get("/{doc_id}")
def get_document(project_id: uuid.UUID, doc_id: uuid.UUID, session: Session = Depends(get_session),
                 user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    return ok(InputSourceOut.model_validate(_get_source(session, project_id, doc_id)))


@router.get("/{doc_id}/evidence")
def get_evidence(project_id: uuid.UUID, doc_id: uuid.UUID, page: int = 1, page_size: int = 100,
                 session: Session = Depends(get_session),
                 user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    _get_source(session, project_id, doc_id)
    page_size = max(1, min(page_size, 500))
    rows = session.execute(
        select(SourceEvidence).where(SourceEvidence.project_id == project_id,
                                     SourceEvidence.input_source_id == doc_id)
        .order_by(SourceEvidence.seq).limit(page_size).offset((max(page, 1) - 1) * page_size)
    ).scalars().all()
    return ok([EvidenceOut.model_validate(r) for r in rows], meta={"page": page})


@router.get("/{doc_id}/transcript")
def get_transcript(project_id: uuid.UUID, doc_id: uuid.UUID, session: Session = Depends(get_session),
                   user: CurrentUser = Depends(current_user)):
    get_member_project(session, user, project_id)
    source = _get_source(session, project_id, doc_id)
    if source.processing_class != ProcessingClass.audio:
        raise NotFoundError("Document is not an audio source")
    if source.extraction_status != ExtractionStatus.succeeded or not source.transcript_path:
        raise ConflictError("Audio has not been transcribed yet",
                            {"status": source.extraction_status.value})
    storage = LocalStorage(get_settings().storage_path)
    segments = json.loads(storage.read_text(source.transcript_segments_path))
    return ok({"text": storage.read_text(source.transcript_path),
               "quality": source.transcript_quality.value if source.transcript_quality else None,
               "language": source.transcription_language,
               "duration_seconds": source.duration_seconds, "model": source.transcription_model,
               "segments": segments["segments"], "excluded_segments": segments["flagged"]})
