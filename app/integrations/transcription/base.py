"""Transcription provider interface + implementations (Groq Whisper, fake for tests)."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.processors.audio.transcript import AsrSegment


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TranscriptionResult:
    segments: tuple[AsrSegment, ...]
    language: str | None
    duration_seconds: float


class TranscriptionProvider(Protocol):
    model_id: str

    def transcribe(self, path: Path, language: str | None = None,
                   prompt: str | None = None) -> TranscriptionResult: ...


class FakeTranscriptionProvider:
    """Deterministic provider for tests: returns queued results, records every call."""

    model_id = "fake-whisper"

    def __init__(self, results: list[TranscriptionResult] | None = None,
                 error: TranscriptionError | None = None):
        self._results = list(results or [])
        self._error = error
        self.calls: list[dict] = []

    def transcribe(self, path: Path, language: str | None = None,
                   prompt: str | None = None) -> TranscriptionResult:
        self.calls.append({"path": Path(path), "language": language, "prompt": prompt})
        if self._error:
            raise self._error
        if not self._results:
            return TranscriptionResult((), language, 0.0)
        return self._results.pop(0)
