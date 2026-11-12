"""Provider factory. Tests inject a fake through `set_provider_override`."""
from app.core.config import Settings
from app.integrations.transcription.base import TranscriptionProvider

_override: TranscriptionProvider | None = None


def set_provider_override(provider: TranscriptionProvider | None) -> None:
    global _override
    _override = provider


def get_transcription_provider(settings: Settings) -> TranscriptionProvider:
    if _override is not None:
        return _override
    if settings.groq_api_key is None:
        raise RuntimeError("GROQ_API_KEY is not configured")
    from app.integrations.transcription.groq_whisper import GroqWhisperProvider

    return GroqWhisperProvider(settings.groq_api_key.get_secret_value(),
                               settings.groq_transcription_model, settings.groq_timeout_seconds)
