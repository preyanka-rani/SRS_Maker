"""Groq Whisper via the OpenAI-compatible transcription endpoint.

Callers MUST gate this behind `audio_processing` consent and run it only from the `asr`
Celery queue (rate limited). Transient errors are surfaced as retryable.
"""
from pathlib import Path

from app.integrations.transcription.base import TranscriptionError, TranscriptionResult
from app.processors.audio.transcript import AsrSegment

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class GroqWhisperProvider:
    def __init__(self, api_key: str, model_id: str = "whisper-large-v3", timeout: float = 300.0):
        from groq import Groq

        self.model_id = model_id
        self._client = Groq(api_key=api_key, timeout=timeout, max_retries=0)

    def transcribe(self, path: Path, language: str | None = None,
                   prompt: str | None = None) -> TranscriptionResult:
        import groq

        kwargs: dict = {"model": self.model_id, "response_format": "verbose_json",
                        "temperature": 0.0}
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt[-224:]  # Whisper only uses the last ~224 tokens
        try:
            with open(path, "rb") as fh:
                response = self._client.audio.transcriptions.create(
                    file=(Path(path).name, fh.read()), **kwargs
                )
        except groq.APIStatusError as exc:
            raise TranscriptionError(
                f"groq_http_{exc.status_code}", "Transcription request failed",
                retryable=exc.status_code in _RETRYABLE_STATUS,
            ) from exc
        except (groq.APIConnectionError, groq.APITimeoutError) as exc:
            raise TranscriptionError("groq_connection", "Cannot reach Groq", retryable=True) from exc
        return _to_result(response)


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _to_result(response) -> TranscriptionResult:
    segments = tuple(
        AsrSegment(
            start=float(_field(s, "start", 0.0)),
            end=float(_field(s, "end", 0.0)),
            text=str(_field(s, "text", "")).strip(),
            avg_logprob=_field(s, "avg_logprob"),
            no_speech_prob=_field(s, "no_speech_prob"),
        )
        for s in (_field(response, "segments") or [])
    )
    duration = float(_field(response, "duration", 0.0) or (segments[-1].end if segments else 0.0))
    return TranscriptionResult(segments, _field(response, "language"), duration)
