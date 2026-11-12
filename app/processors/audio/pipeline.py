"""Audio -> timestamped transcript. Provider- and ffmpeg-agnostic orchestration."""
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.database.models.sources import TranscriptQuality
from app.integrations.transcription.base import TranscriptionProvider
from app.processors.audio import ffmpeg_tools as ff
from app.processors.audio.planner import max_chunk_seconds, plan_chunks
from app.processors.audio.transcript import (
    AsrSegment,
    ChunkTranscript,
    assess_quality,
    stitch,
)

CONTEXT_CHARS = 200


@dataclass(frozen=True)
class AudioConfig:
    max_minutes: float = 90
    chunk_max_seconds: float = 600
    max_file_mb: float = 24
    silence_noise_db: float = -35
    silence_min_seconds: float = 0.4
    silence_search_window: float = 120
    forced_cut_overlap: float = 2
    no_speech_threshold: float = 0.6
    min_avg_logprob: float = -1.0
    low_confidence_ratio: float = 0.3
    language_hint: str | None = None


@dataclass(frozen=True)
class TranscriptionOutcome:
    segments: tuple[AsrSegment, ...]  # usable segments with absolute timestamps
    flagged: tuple[AsrSegment, ...]
    quality: TranscriptQuality
    language: str | None
    duration_seconds: float
    chunk_count: int
    cut_types: tuple[str, ...]


def transcribe_file(src: Path, provider: TranscriptionProvider, cfg: AudioConfig,
                    workdir: Path | None = None) -> TranscriptionOutcome:
    probe = ff.probe(src)
    if probe.duration_seconds > cfg.max_minutes * 60:
        raise ff.AudioError("audio_too_long", f"Audio exceeds {cfg.max_minutes:g} minutes")
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        tmpdir = Path(tmp)
        norm = tmpdir / "norm.flac"
        ff.normalize(src, norm)
        total = ff.probe(norm).duration_seconds or probe.duration_seconds
        max_bytes = int(cfg.max_file_mb * 1024 * 1024)
        bps = norm.stat().st_size / total if total else 0
        ceiling = max_chunk_seconds(cfg.chunk_max_seconds, max_bytes, bps)

        chunk_paths: list[tuple[Path, object]] = []
        if total <= ceiling and norm.stat().st_size <= max_bytes:
            planned = plan_chunks(total, [], ceiling, cfg.silence_search_window)
            chunk_paths = [(norm, planned[0])]
        else:
            silences = ff.detect_silences(norm, cfg.silence_noise_db, cfg.silence_min_seconds, total)

            def redetect(low: float, high: float):
                return ff.detect_silences(norm, cfg.silence_noise_db + 5,
                                          cfg.silence_min_seconds / 2, total, low, high)

            planned = _plan_with_size_check(norm, tmpdir, total, silences, ceiling, cfg, max_bytes,
                                            redetect, chunk_paths)
        transcripts: list[ChunkTranscript] = []
        previous_text = ""
        language = cfg.language_hint
        for path, chunk in chunk_paths:
            result = provider.transcribe(path, language=cfg.language_hint,
                                         prompt=previous_text[-CONTEXT_CHARS:] or None)
            language = language or result.language
            transcripts.append(ChunkTranscript(chunk.start, chunk.cut_type, chunk.overlap_before,
                                               result.segments))
            previous_text = " ".join(s.text for s in result.segments)
        stitched = stitch(transcripts)
        usable, flagged, quality = assess_quality(
            stitched, cfg.no_speech_threshold, cfg.min_avg_logprob, cfg.low_confidence_ratio
        )
        return TranscriptionOutcome(tuple(usable), tuple(flagged), quality, language, total,
                                    len(chunk_paths), tuple(c.cut_type for _, c in chunk_paths))


def _plan_with_size_check(norm, tmpdir, total, silences, ceiling, cfg, max_bytes, redetect,
                          chunk_paths):
    """Cut chunks and re-split any that still exceed the upload limit (min 30 s)."""
    from app.processors.audio.planner import MIN_CHUNK_SECONDS, PlannedChunk

    planned = plan_chunks(total, silences, ceiling, cfg.silence_search_window,
                          cfg.forced_cut_overlap, redetect)
    queue = list(planned)
    index = 0
    while queue:
        chunk = queue.pop(0)
        out = tmpdir / f"chunk_{index:04d}.flac"
        ff.cut(norm, out, chunk.start, chunk.end)
        if out.stat().st_size > max_bytes and (chunk.end - chunk.start) > 2 * MIN_CHUNK_SECONDS:
            # re-plan inside this chunk using its own silences
            inner = [s for s in silences if s.start >= chunk.start and s.end <= chunk.end]
            sub = plan_chunks(chunk.end - chunk.start,
                              [type(s)(s.start - chunk.start, s.end - chunk.start) for s in inner],
                              (chunk.end - chunk.start) / 2, cfg.silence_search_window,
                              cfg.forced_cut_overlap, None)
            queue = [PlannedChunk(chunk.start + p.start, chunk.start + p.end,
                                  p.cut_type if p.cut_type != "end" else chunk.cut_type,
                                  chunk.overlap_before if p is sub[0] else p.overlap_before)
                     for p in sub] + queue
            out.unlink(missing_ok=True)
            continue
        chunk_paths.append((out, chunk))
        index += 1
    return planned
