import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import (
    ExtractionStatus,
    IssueType,
    ManualReview,
    ProjectLlmConsent,
    RequirementIssue,
    SourceEvidence,
    SourceType,
    TranscriptQuality,
)
from app.integrations.storage.local import LocalStorage
from app.integrations.transcription.base import (
    FakeTranscriptionProvider,
    TranscriptionError,
    TranscriptionResult,
)
from app.processors.audio.transcript import AsrSegment
from app.services import ingestion_service as svc
from tests.helpers import docx_bytes, make_audio, make_project, minimal_pdf


@pytest.fixture
def env(tmp_path, db):
    settings = Settings(storage_root=str(tmp_path / "store"), _env_file=None)
    return settings, LocalStorage(settings.storage_path), make_project(db)


def _issues(db, project, itype=None):
    q = select(RequirementIssue).where(RequirementIssue.project_id == project.id)
    if itype:
        q = q.where(RequirementIssue.issue_type == itype)
    return db.execute(q).scalars().all()


def _reviews(db, project):
    return db.execute(select(ManualReview).where(ManualReview.project_id == project.id)).scalars().all()


def _register(env, db, data, name, **kw):
    settings, storage, project = env
    return svc.register_source(db, storage, settings, project.id, data, name,
                               kw.pop("source_type", SourceType.upload), **kw)


def test_docx_is_extracted_into_ordered_verbatim_evidence(env, db, tmp_path):
    settings, storage, project = env
    data = docx_bytes(tmp_path, ["The system shall allow login.", "Admins can export reports."])
    res = _register(env, db, data, "req.docx")
    assert res.created
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    assert src.extraction_status == ExtractionStatus.succeeded
    rows = db.execute(select(SourceEvidence).where(SourceEvidence.input_source_id == src.id)
                      .order_by(SourceEvidence.seq)).scalars().all()
    assert [r.excerpt for r in rows] == ["The system shall allow login.",
                                         "Admins can export reports."]
    assert rows[0].locator_paragraph == 1 and rows[1].char_start > rows[0].char_end - 1
    assert storage.path(src.storage_path).exists() and "inputs" in src.storage_path


def test_pdf_pages_become_evidence_with_page_locators(env, db):
    settings, storage, _ = env
    res = _register(env, db, minimal_pdf(["Users must reset passwords", "Audit every login"]),
                    "spec.pdf")
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    assert src.extraction_status == ExtractionStatus.succeeded and src.page_count == 2
    pages = db.execute(select(SourceEvidence.locator_page, SourceEvidence.excerpt)
                       .where(SourceEvidence.input_source_id == src.id)
                       .order_by(SourceEvidence.seq)).all()
    assert pages == [(1, "Users must reset passwords"), (2, "Audit every login")]


def test_scanned_pdf_is_flagged_needs_ocr_with_blocking_issue(env, db):
    settings, storage, project = env
    res = _register(env, db, minimal_pdf(["", ""]), "scan.pdf")
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    assert src.extraction_status == ExtractionStatus.needs_ocr
    assert any(i.blocking for i in _issues(db, project, IssueType.unprocessed_attachment))
    assert any(r.reason_code == "NEEDS_OCR" for r in _reviews(db, project))


def test_duplicate_upload_returns_original(env, db, tmp_path):
    data = docx_bytes(tmp_path, ["same"])
    first = _register(env, db, data, "a.docx")
    second = _register(env, db, data, "renamed.docx")
    assert first.created and not second.created and first.source.id == second.source.id


def test_corrupt_docx_fails_with_review_not_crash(env, db):
    settings, storage, project = env
    res = _register(env, db, b"PK\x03\x04 not really a zip", "broken.docx")
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    assert src.extraction_status == ExtractionStatus.failed
    assert "corrupt_docx" in src.extraction_error
    assert any(r.reason_code == "EXTRACTION_FAILED" for r in _reviews(db, project))


def test_extension_mismatch_is_rejected(env, db):
    settings, storage, _ = env
    res = _register(env, db, b"plain text pretending", "fake.pdf")
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    assert src.extraction_status == ExtractionStatus.failed
    assert "extension_mismatch" in src.extraction_error


def test_invisible_characters_are_stripped_and_flagged(env, db):
    settings, storage, project = env
    res = _register(env, db, "Login‮ must work​ today".encode(), "n.txt")
    src = svc.ingest_text_source(db, storage, settings, res.source.id)
    text = db.execute(select(SourceEvidence.excerpt)
                      .where(SourceEvidence.input_source_id == src.id)).scalar_one()
    assert text == "Login must work today"
    assert _issues(db, project, IssueType.injection_suspected)


@pytest.mark.parametrize("name,mime,status", [
    ("demo.mp4", "video/mp4", ExtractionStatus.deferred_unsupported),
    ("sheet.xlsx", None, ExtractionStatus.deferred_unsupported),
    ("bundle.zip", "application/zip", ExtractionStatus.deferred_unsupported),
    ("photo.jpg", "image/jpeg", ExtractionStatus.deferred_unsupported),
    ("run.exe", None, ExtractionStatus.rejected_dangerous),
])
def test_non_text_attachments_never_silently_ignored(env, db, name, mime, status):
    _, _, project = env
    res = _register(env, db, b"\x00binary-bytes" + name.encode(), name, mime=mime,
                    source_type=SourceType.email_attachment)
    assert res.source.extraction_status == status
    blocking = [i for i in _issues(db, project) if i.blocking]
    assert blocking, "an unprocessed attachment must raise a blocking issue"
    assert any(r.reason_code in {"UNPROCESSED_ATTACHMENT", "ATTACHMENT_REJECTED"}
               for r in _reviews(db, project))


def test_inline_signature_image_is_ignored_quietly(env, db):
    _, _, project = env
    res = _register(env, db, b"\x89PNG-logo", "logo.png", mime="image/png", is_inline=True,
                    source_type=SourceType.email_attachment)
    assert res.source.extraction_status == ExtractionStatus.succeeded
    assert not _issues(db, project)


def test_oversized_attachment_rejected_by_policy(env, db):
    settings, storage, project = env
    tiny = Settings(storage_root=settings.storage_root, max_attachment_mb=0, _env_file=None)
    res = svc.register_source(db, storage, tiny, project.id, b"x" * 2048, "big.pdf",
                              SourceType.upload)
    assert res.source.extraction_status == ExtractionStatus.rejected_policy


# ---------------- audio ----------------
def _audio_source(env, db, tmp_path, plan=(("tone", 12),)):
    path = make_audio(tmp_path / "voice.wav", list(plan))
    return _register(env, db, path.read_bytes(), "voice.wav",
                     source_type=SourceType.email_attachment).source


def _grant_audio(db, project):
    db.add(ProjectLlmConsent(project_id=project.id, scope="audio_processing",
                             policy_reference="DPA-AUDIO-1"))
    db.flush()


def test_audio_without_consent_is_held_not_sent(env, db, tmp_path):
    settings, storage, project = env
    src = _audio_source(env, db, tmp_path)
    provider = FakeTranscriptionProvider()
    out = svc.transcribe_source(db, storage, settings, provider, src.id)
    assert out.extraction_status == ExtractionStatus.awaiting_consent
    assert provider.calls == []  # nothing leaves the system without consent
    assert any(r.reason_code == "LLM_CONSENT_REQUIRED" for r in _reviews(db, project))


def test_audio_transcribed_with_timestamped_evidence(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    src = _audio_source(env, db, tmp_path)
    provider = FakeTranscriptionProvider([TranscriptionResult(
        (AsrSegment(0.0, 4.5, "We need single sign on", -0.2, 0.01),
         AsrSegment(4.5, 11.0, "and an audit trail", -0.3, 0.02)), "en", 12.0)])
    out = svc.transcribe_source(db, storage, settings, provider, src.id)
    assert out.extraction_status == ExtractionStatus.succeeded
    assert out.transcript_quality == TranscriptQuality.ok and out.transcription_language == "en"
    rows = db.execute(select(SourceEvidence).where(SourceEvidence.input_source_id == src.id)
                      .order_by(SourceEvidence.seq)).scalars().all()
    assert [r.excerpt for r in rows] == ["We need single sign on", "and an audit trail"]
    assert (rows[0].locator_time_start_ms, rows[0].locator_time_end_ms) == (0, 4500)
    assert "single sign on" in storage.read_text(out.transcript_path)


def test_hallucinated_silence_segments_are_excluded(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    src = _audio_source(env, db, tmp_path)
    provider = FakeTranscriptionProvider([TranscriptionResult(
        (AsrSegment(0, 5, "Real requirement text", -0.2, 0.02),
         AsrSegment(5, 8, "Thanks for watching!", -1.8, 0.95)), "en", 12.0)])
    svc.transcribe_source(db, storage, settings, provider, src.id)
    texts = db.execute(select(SourceEvidence.excerpt)
                       .where(SourceEvidence.input_source_id == src.id)).scalars().all()
    assert texts == ["Real requirement text"]


def test_silent_audio_yields_blocking_issue(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    src = _audio_source(env, db, tmp_path)
    provider = FakeTranscriptionProvider([TranscriptionResult(
        (AsrSegment(0, 12, "you", -2.0, 0.99),), "en", 12.0)])
    out = svc.transcribe_source(db, storage, settings, provider, src.id)
    assert out.transcript_quality == TranscriptQuality.no_speech
    assert any(i.blocking for i in _issues(db, project, IssueType.low_confidence_transcript))


def test_corrupt_audio_fails_cleanly_and_never_calls_provider(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    res = _register(env, db, b"definitely not audio bytes" * 20, "bad.mp3",
                    source_type=SourceType.email_attachment)
    provider = FakeTranscriptionProvider()
    out = svc.transcribe_source(db, storage, settings, provider, res.source.id)
    assert out.extraction_status == ExtractionStatus.failed and provider.calls == []


def test_transient_provider_error_is_retryable_and_state_recovers(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    src = _audio_source(env, db, tmp_path)
    flaky = FakeTranscriptionProvider(error=TranscriptionError("groq_http_429", "rate", retryable=True))
    with pytest.raises(TranscriptionError):
        svc.transcribe_source(db, storage, settings, flaky, src.id)
    assert src.extraction_status == ExtractionStatus.pending  # safe to retry


def test_permanent_provider_error_fails_source(env, db, tmp_path):
    settings, storage, project = env
    _grant_audio(db, project)
    src = _audio_source(env, db, tmp_path)
    dead = FakeTranscriptionProvider(error=TranscriptionError("groq_http_401", "bad key"))
    out = svc.transcribe_source(db, storage, settings, dead, src.id)
    assert out.extraction_status == ExtractionStatus.failed


def test_pending_sources_are_ordered_oldest_first(env, db, tmp_path):
    settings, storage, project = env
    ids = []
    for i in range(3):
        res = _register(env, db, docx_bytes(tmp_path, [f"para {i}"], f"d{i}.docx"), f"d{i}.docx")
        svc.ingest_text_source(db, storage, settings, res.source.id)
        ids.append(res.source.id)
    assert [s.id for s in svc.pending_text_sources(db, project.id)] == ids
