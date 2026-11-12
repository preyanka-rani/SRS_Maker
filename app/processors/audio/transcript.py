"""Stitching chunk transcripts and quality flagging (pure functions)."""
import re
from dataclasses import dataclass, replace

from app.database.models.sources import TranscriptQuality


@dataclass(frozen=True)
class AsrSegment:
    start: float  # seconds
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True)
class ChunkTranscript:
    chunk_start: float
    cut_type: str  # how this chunk's END was cut
    overlap_before: float
    segments: tuple[AsrSegment, ...]


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def stitch(chunks: list[ChunkTranscript]) -> list[AsrSegment]:
    """Offset local timestamps; dedupe words that were re-covered by a forced-cut overlap."""
    result: list[AsrSegment] = []
    for chunk in chunks:
        shifted = [
            replace(s, start=s.start + chunk.chunk_start, end=s.end + chunk.chunk_start)
            for s in chunk.segments
        ]
        if chunk.overlap_before > 0 and result:
            tail = " ".join(_norm(s.text) for s in result[-3:])
            boundary = chunk.chunk_start + chunk.overlap_before
            kept = []
            for seg in shifted:
                inside_overlap = seg.end <= boundary + 0.5
                if inside_overlap and _norm(seg.text) and _norm(seg.text) in tail:
                    continue
                kept.append(seg)
            shifted = kept
        result.extend(shifted)
    return result


def assess_quality(
    segments: list[AsrSegment],
    no_speech_threshold: float,
    min_avg_logprob: float,
    low_confidence_ratio: float,
) -> tuple[list[AsrSegment], list[AsrSegment], TranscriptQuality]:
    """Split into (usable, flagged) and rate the transcript as a whole."""
    usable: list[AsrSegment] = []
    flagged: list[AsrSegment] = []
    for seg in segments:
        bad = (seg.no_speech_prob is not None and seg.no_speech_prob > no_speech_threshold) or (
            seg.avg_logprob is not None and seg.avg_logprob < min_avg_logprob
        )
        (flagged if bad or not seg.text.strip() else usable).append(seg)
    if not usable:
        return usable, flagged, TranscriptQuality.no_speech
    total = len(usable) + len(flagged)
    if total and len(flagged) / total > low_confidence_ratio:
        return usable, flagged, TranscriptQuality.low_confidence
    return usable, flagged, TranscriptQuality.ok
