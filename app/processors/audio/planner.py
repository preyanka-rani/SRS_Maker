"""Silence-based chunk planning (pure functions; no ffmpeg here).

Cuts are placed at the midpoint of the longest silence just below the chunk ceiling so a
word is never split. A forced cut with a small overlap is used only when no pause exists.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass

MIN_CHUNK_SECONDS = 30.0
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class PlannedChunk:
    start: float
    end: float
    cut_type: str  # "silence" | "forced" | "end"
    overlap_before: float = 0.0  # seconds this chunk re-covers from the previous one


def parse_silencedetect(stderr: str, total_duration: float) -> list[Silence]:
    """Parse ffmpeg `silencedetect` log lines. A trailing open silence ends at total_duration."""
    silences: list[Silence] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        m_start = _SILENCE_START.search(line)
        if m_start:
            open_start = max(0.0, float(m_start.group(1)))
            continue
        m_end = _SILENCE_END.search(line)
        if m_end and open_start is not None:
            silences.append(Silence(open_start, min(float(m_end.group(1)), total_duration)))
            open_start = None
    if open_start is not None:
        silences.append(Silence(open_start, total_duration))
    return silences


def max_chunk_seconds(chunk_max_seconds: float, max_file_bytes: int, bytes_per_second: float,
                      safety: float = 0.9) -> float:
    """Largest chunk that respects both the time ceiling and the upload size limit."""
    by_size = (max_file_bytes * safety) / bytes_per_second if bytes_per_second > 0 else chunk_max_seconds
    return max(MIN_CHUNK_SECONDS, min(chunk_max_seconds, by_size))


def _best_silence(silences: list[Silence], low: float, high: float, start: float) -> float | None:
    """Midpoint of the longest silence inside [low, high]; ties go to the latest."""
    best: tuple[float, float, float] | None = None  # (duration, midpoint, cut)
    for s in silences:
        lo, hi = max(s.start, low), min(s.end, high)
        if hi <= lo:
            continue
        cut = (lo + hi) / 2
        if cut <= start + MIN_CHUNK_SECONDS / 2:
            continue
        key = (hi - lo, cut)
        if best is None or key >= (best[0], best[1]):
            best = (hi - lo, cut, cut)
    return best[2] if best else None


def plan_chunks(
    total_duration: float,
    silences: list[Silence],
    max_chunk: float,
    search_window: float,
    forced_overlap: float = 2.0,
    redetect: Callable[[float, float], list[Silence]] | None = None,
) -> list[PlannedChunk]:
    """Split [0, total_duration] into chunks no longer than `max_chunk` seconds."""
    if total_duration <= max_chunk:
        return [PlannedChunk(0.0, total_duration, "end")]
    chunks: list[PlannedChunk] = []
    start = 0.0
    overlap = 0.0
    while total_duration - start > max_chunk:
        ceiling = start + max_chunk
        low = max(start + MIN_CHUNK_SECONDS, ceiling - search_window)
        cut = _best_silence(silences, low, ceiling, start)
        if cut is None and redetect is not None:
            cut = _best_silence(redetect(low, ceiling), low, ceiling, start)
        if cut is not None:
            chunks.append(PlannedChunk(start, cut, "silence", overlap))
            start, overlap = cut, 0.0
        else:
            chunks.append(PlannedChunk(start, ceiling, "forced", overlap))
            start, overlap = ceiling - forced_overlap, forced_overlap
    chunks.append(PlannedChunk(start, total_duration, "end", overlap))
    return chunks
