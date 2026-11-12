import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class InputSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    source_type: str
    original_filename: str | None
    processing_class: str
    extraction_status: str
    extraction_error: str | None
    size_bytes: int
    sha256: str
    page_count: int | None
    char_count: int | None
    duration_seconds: float | None
    transcript_quality: str | None
    transcription_language: str | None
    created_at: datetime


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    excerpt: str
    locator_page: int | None
    locator_paragraph: int | None
    locator_time_start_ms: int | None
    locator_time_end_ms: int | None
    language: str | None
