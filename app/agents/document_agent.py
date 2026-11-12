"""Register sources, extract text into verbatim evidence, transcribe audio.

Everything here is deterministic and local, except `transcribe_source`, which calls the
transcription provider (Groq) and therefore requires `audio_processing` consent.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    ConsentScope,
    ExtractionStatus,
    InputSource,
    IssueType,
    LlmCall,
    ProcessingClass,
    SourceEvidence,
    SourceType,
)
from app.integrations.storage.local import LocalStorage
from app.integrations.transcription.base import TranscriptionError, TranscriptionProvider
from app.processors.attachment_classifier import AttachmentInfo, classify
from app.processors.audio.ffmpeg_tools import AudioError
from app.processors.audio.pipeline import AudioConfig, transcribe_file
from app.processors.extractors import ExtractionError, extract
from app.processors.file_validation import FileRejected, validate_document
from app.services import issue_service
from app.services.llm_service import has_consent

_DEFERRED = {
    ProcessingClass.video: "video",
    ProcessingClass.image: "image",
    ProcessingClass.office_other: "office document",
    ProcessingClass.archive: "archive (not unpacked for security)",
    ProcessingClass.unknown: "file of unknown type",
}


@dataclass(frozen=True)
class RegisterResult:
    source: InputSource
    created: bool


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def audio_config(settings: Settings) -> AudioConfig:
    return AudioConfig(
        max_minutes=settings.max_audio_minutes, chunk_max_seconds=settings.audio_chunk_max_seconds,
        max_file_mb=settings.groq_max_file_mb, silence_noise_db=settings.audio_silence_noise_db,
        silence_min_seconds=settings.audio_silence_min_seconds,
        silence_search_window=settings.audio_silence_search_window_seconds,
        forced_cut_overlap=settings.audio_forced_cut_overlap_seconds,
        no_speech_threshold=settings.asr_no_speech_threshold,
        min_avg_logprob=settings.asr_min_avg_logprob,
        low_confidence_ratio=settings.asr_low_confidence_ratio,
        language_hint=settings.transcription_language_hint or None,
    )


def register_source(session: Session, storage: LocalStorage, settings: Settings,
                    project_id: uuid.UUID, data: bytes, filename: str | None,
                    source_type: SourceType, *, mime: str | None = None, is_inline: bool = False,
                    email_message_id: uuid.UUID | None = None,
                    parent_id: uuid.UUID | None = None,
                    uploaded_by: uuid.UUID | None = None) -> RegisterResult:
    """Store + classify any file. Duplicates (same project, sha256, type) return the original."""
    sha = hashlib.sha256(data).hexdigest()
    existing = session.execute(
        select(InputSource).where(InputSource.project_id == project_id,
                                  InputSource.sha256 == sha,
                                  InputSource.source_type == source_type)
    ).scalar_one_or_none()
    if existing:
        return RegisterResult(existing, False)

    cls = classify(AttachmentInfo(filename, mime, is_inline, len(data)))
    suffix = Path(filename or "").suffix
    rel = storage.save_bytes("quarantine", project_id, data, suffix)
    source = InputSource(
        project_id=project_id, parent_input_source_id=parent_id, source_type=source_type,
        email_message_id=email_message_id, original_filename=filename, mime_declared=mime,
        is_inline=is_inline, size_bytes=len(data), sha256=sha, processing_class=cls,
        storage_path=rel, uploaded_by=uploaded_by, extraction_status=ExtractionStatus.pending,
    )
    session.add(source)
    session.flush()

    limit = settings.max_attachment_mb * 1024 * 1024
    if cls == ProcessingClass.dangerous:
        _reject(session, source, ExtractionStatus.rejected_dangerous, IssueType.unprocessed_attachment,
                f"Dangerous attachment rejected: {filename}", "dangerous_file_type")
    elif len(data) > limit and cls != ProcessingClass.inline_decoration:
        _reject(session, source, ExtractionStatus.rejected_policy, IssueType.unprocessed_attachment,
                f"Attachment exceeds {settings.max_attachment_mb} MB: {filename}", "too_large")
    elif cls == ProcessingClass.inline_decoration:
        source.extraction_status = ExtractionStatus.succeeded
        source.consumed_by_workflow_at = _now()
    elif cls in _DEFERRED:
        what = _DEFERRED[cls]
        source.extraction_status = ExtractionStatus.deferred_unsupported
        issue_service.raise_issue(
            session, project_id, IssueType.unprocessed_attachment,
            f"Unprocessed attachment ({what}): {filename or 'unnamed'}", blocking=True,
            description=f"A {what} was received but cannot be processed automatically in this "
                        "version. A person must review it and add any requirements by hand.",
            why_it_matters="Its content may contain requirements that are not yet reflected.",
            key=f"src:{source.id}", related_input_source_id=source.id)
        issue_service.open_manual_review(session, project_id, "UNPROCESSED_ATTACHMENT",
                                         details=f"{what}: {filename}", entity_type="input_source",
                                         entity_id=str(source.id))
    session.flush()
    return RegisterResult(source, True)


def _reject(session: Session, source: InputSource, status: ExtractionStatus, issue_type: IssueType,
            title: str, code: str) -> None:
    source.extraction_status = status
    source.extraction_error = code
    issue_service.raise_issue(session, source.project_id, issue_type, title, blocking=True,
                              key=f"src:{source.id}", related_input_source_id=source.id,
                              description=f"Reason: {code}.")
    issue_service.open_manual_review(session, source.project_id, "ATTACHMENT_REJECTED",
                                     details=title, entity_type="input_source",
                                     entity_id=str(source.id))


def _fail(session: Session, source: InputSource, code: str, message: str) -> InputSource:
    source.extraction_status = ExtractionStatus.failed
    source.extraction_error = f"{code}: {message}"
    issue_service.raise_issue(
        session, source.project_id, IssueType.unprocessed_attachment,
        f"Could not process {source.original_filename or 'source'}", blocking=True,
        description=message, key=f"src:{source.id}", related_input_source_id=source.id)
    issue_service.open_manual_review(session, source.project_id, "EXTRACTION_FAILED",
                                     details=message, entity_type="input_source",
                                     entity_id=str(source.id))
    session.flush()
    return source


def _write_evidence(session: Session, source: InputSource, items: list[dict]) -> int:
    for seq, item in enumerate(items, start=1):
        session.add(SourceEvidence(
            project_id=source.project_id, input_source_id=source.id, seq=seq,
            excerpt=item["text"], content_sha256=hashlib.sha256(item["text"].encode()).hexdigest(),
            locator_page=item.get("page"), locator_paragraph=item.get("paragraph"),
            char_start=item.get("char_start"), char_end=item.get("char_end"),
            locator_time_start_ms=item.get("t0"), locator_time_end_ms=item.get("t1"),
            language=item.get("language"), asr_avg_logprob=item.get("logprob"),
            asr_no_speech_prob=item.get("no_speech")))
    session.flush()
    return len(items)


def ingest_text_source(session: Session, storage: LocalStorage, settings: Settings,
                       source_id: uuid.UUID) -> InputSource:
    """Validate + extract a PDF/DOCX/TXT source into evidence rows. Idempotent."""
    source = session.get(InputSource, source_id)
    if source is None:
        raise ValueError("unknown source")
    if source.extraction_status == ExtractionStatus.succeeded:
        return source
    source.extraction_status = ExtractionStatus.processing
    session.flush()
    path = storage.path(source.storage_path)
    try:
        # an extension the sender chose is untrusted: validate against sniffed content
        validated = validate_document(path, settings.max_attachment_mb)
        doc = extract(path, validated.detected_kind)
    except FileRejected as exc:
        return _fail(session, source, exc.code, str(exc))
    except ExtractionError as exc:
        return _fail(session, source, exc.code, str(exc))

    source.mime_detected = validated.detected_kind
    source.page_count = doc.page_count
    source.char_count = doc.char_count
    if doc.needs_ocr:
        source.extraction_status = ExtractionStatus.needs_ocr
        issue_service.raise_issue(
            session, source.project_id, IssueType.unprocessed_attachment,
            f"Scanned document needs OCR: {source.original_filename}", blocking=True,
            description="Little or no selectable text was found. OCR is not available yet.",
            key=f"src:{source.id}", related_input_source_id=source.id)
        issue_service.open_manual_review(session, source.project_id, "NEEDS_OCR",
                                         entity_type="input_source", entity_id=str(source.id))
        return source
    if not doc.segments:
        return _fail(session, source, "no_text", "No text could be extracted")

    items, offset, parts = [], 0, []
    for seg in doc.segments:
        items.append({"text": seg.text, "page": seg.page, "paragraph": seg.paragraph,
                      "char_start": offset, "char_end": offset + len(seg.text)})
        parts.append(seg.text)
        offset += len(seg.text) + 2
    source.extracted_text_path = storage.save_text("inputs", source.project_id, "\n\n".join(parts))
    source.storage_path = storage.move(source.storage_path, "inputs", source.project_id)
    _write_evidence(session, source, items)
    source.extraction_status = ExtractionStatus.succeeded
    if doc.flags:
        issue_service.raise_issue(
            session, source.project_id, IssueType.injection_suspected,
            f"Hidden/invisible characters removed from {source.original_filename}",
            description=", ".join(doc.flags), key=f"flags:{source.id}",
            related_input_source_id=source.id)
    session.flush()
    return source


def transcribe_source(session: Session, storage: LocalStorage, settings: Settings,
                      provider: TranscriptionProvider, source_id: uuid.UUID) -> InputSource:
    """Audio -> transcript + timestamped evidence. Requires audio_processing consent."""
    source = session.get(InputSource, source_id)
    if source is None or source.processing_class != ProcessingClass.audio:
        raise ValueError("not an audio source")
    if source.extraction_status == ExtractionStatus.succeeded:
        return source
    if not has_consent(session, source.project_id, ConsentScope.audio_processing):
        source.extraction_status = ExtractionStatus.awaiting_consent
        issue_service.raise_issue(
            session, source.project_id, IssueType.unprocessed_attachment,
            f"Audio awaiting processing consent: {source.original_filename}", blocking=True,
            description="Audio is not sent to the transcription service until audio_processing "
                        "consent is granted for this project.",
            key=f"src:{source.id}", related_input_source_id=source.id)
        issue_service.open_manual_review(session, source.project_id, "LLM_CONSENT_REQUIRED",
                                         details="audio_processing", entity_type="input_source",
                                         entity_id=str(source.id))
        session.flush()
        return source

    source.extraction_status = ExtractionStatus.transcribing
    session.flush()
    cfg = audio_config(settings)
    try:
        outcome = transcribe_file(storage.path(source.storage_path), provider, cfg,
                                  workdir=storage.root / "temp")
    except AudioError as exc:
        return _fail(session, source, exc.code, str(exc))
    except TranscriptionError as exc:
        if exc.retryable:
            source.extraction_status = ExtractionStatus.pending
            session.flush()
            raise
        return _fail(session, source, exc.code, str(exc))

    session.add(LlmCall(project_id=source.project_id, provider="groq", purpose="transcription",
                        model_id=provider.model_id, status="ok",
                        audio_seconds=outcome.duration_seconds))
    source.duration_seconds = outcome.duration_seconds
    source.transcription_model = provider.model_id
    source.transcription_language = outcome.language
    source.transcript_quality = outcome.quality
    text = "\n".join(s.text for s in outcome.segments)
    source.transcript_path = storage.save_text("transcripts", source.project_id, text)
    segments_json = json.dumps(
        {"chunks": outcome.chunk_count, "cut_types": list(outcome.cut_types),
         "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in outcome.segments],
         "flagged": [{"start": s.start, "end": s.end, "text": s.text} for s in outcome.flagged]},
        ensure_ascii=False)
    source.transcript_segments_path = storage.save_text("transcripts", source.project_id,
                                                        segments_json, ".json")
    source.char_count = len(text)
    items = [{"text": s.text, "t0": int(s.start * 1000), "t1": int(s.end * 1000),
              "language": outcome.language, "logprob": s.avg_logprob, "no_speech": s.no_speech_prob}
             for s in outcome.segments]
    _write_evidence(session, source, items)
    source.storage_path = storage.move(source.storage_path, "inputs", source.project_id)
    source.extraction_status = ExtractionStatus.succeeded

    from app.database.models import TranscriptQuality

    if outcome.quality == TranscriptQuality.no_speech:
        issue_service.raise_issue(
            session, source.project_id, IssueType.low_confidence_transcript,
            f"No usable speech detected in {source.original_filename}", blocking=True,
            key=f"asr:{source.id}", related_input_source_id=source.id)
        issue_service.open_manual_review(session, source.project_id, "EMPTY_TRANSCRIPT",
                                         entity_type="input_source", entity_id=str(source.id))
    elif outcome.quality == TranscriptQuality.low_confidence:
        issue_service.raise_issue(
            session, source.project_id, IssueType.low_confidence_transcript,
            f"Low-confidence transcript: {source.original_filename}", blocking=False,
            description=f"{len(outcome.flagged)} segments were excluded as unreliable.",
            key=f"asr:{source.id}", related_input_source_id=source.id)
    session.flush()
    return source


def pending_text_sources(session: Session, project_id: uuid.UUID) -> list[InputSource]:
    return list(session.execute(
        select(InputSource).where(
            InputSource.project_id == project_id,
            InputSource.extraction_status == ExtractionStatus.succeeded,
            InputSource.consumed_by_workflow_at.is_(None),
            InputSource.processing_class.in_([ProcessingClass.text_supported,
                                              ProcessingClass.audio]))
        .order_by(InputSource.created_at)).scalars())


def sources_awaiting_consent(session: Session, project_id: uuid.UUID) -> list[InputSource]:
    """Audio held because audio_processing consent was missing; re-dispatch after it is granted."""
    return list(session.execute(
        select(InputSource).where(InputSource.project_id == project_id,
                                  InputSource.extraction_status == ExtractionStatus.awaiting_consent)
        .order_by(InputSource.created_at)).scalars())
