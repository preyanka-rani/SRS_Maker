"""Audio: ffprobe/ffmpeg normalise, silence-based chunking, Groq Whisper transcription."""
from app.processors.audio.pipeline import AudioConfig, transcribe_file

__all__ = ["AudioConfig", "transcribe_file"]
