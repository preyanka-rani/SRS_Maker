from app.database.models.sources import TranscriptQuality
from app.processors.audio.planner import (
    MIN_CHUNK_SECONDS,
    Silence,
    max_chunk_seconds,
    parse_silencedetect,
    plan_chunks,
)
from app.processors.audio.transcript import AsrSegment, ChunkTranscript, assess_quality, stitch

LOG = """
[silencedetect @ 0x1] silence_start: 10.5
[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 1.5
[silencedetect @ 0x1] silence_start: 95.25
"""


def test_parse_silencedetect_closes_open_silence_at_end():
    s = parse_silencedetect(LOG, total_duration=100.0)
    assert s == [Silence(10.5, 12.0), Silence(95.25, 100.0)]


def test_short_audio_is_not_chunked():
    chunks = plan_chunks(300, [], max_chunk=600, search_window=120)
    assert len(chunks) == 1 and chunks[0].cut_type == "end"


def test_cuts_at_midpoint_of_longest_silence_near_ceiling():
    silences = [Silence(500, 500.5), Silence(540, 543), Silence(590, 591)]
    chunks = plan_chunks(1000, silences, max_chunk=600, search_window=120)
    assert chunks[0].cut_type == "silence"
    assert chunks[0].end == 541.5  # midpoint of the longest (3 s) silence in [480, 600]
    assert chunks[1].start == 541.5


def test_every_chunk_respects_max_and_chunks_are_contiguous():
    silences = [Silence(t, t + 1.0) for t in range(100, 4000, 90)]
    chunks = plan_chunks(4000, silences, max_chunk=600, search_window=120)
    assert chunks[0].start == 0 and chunks[-1].end == 4000
    for a, b in zip(chunks, chunks[1:]):
        assert a.end == b.start
    assert all(c.end - c.start <= 600 for c in chunks)
    assert all(c.cut_type == "silence" for c in chunks[:-1])


def test_forced_cut_with_overlap_when_no_silence():
    chunks = plan_chunks(1000, [], max_chunk=600, search_window=120, forced_overlap=2)
    assert chunks[0].cut_type == "forced" and chunks[0].end == 600
    assert chunks[1].start == 598 and chunks[1].overlap_before == 2


def test_relaxed_redetection_finds_silence_before_forcing_cut():
    calls = []

    def redetect(low, high):
        calls.append((low, high))
        return [Silence(560, 561)]

    chunks = plan_chunks(1000, [], 600, 120, redetect=redetect)
    assert calls and chunks[0].cut_type == "silence" and chunks[0].end == 560.5


def test_silence_too_close_to_chunk_start_is_ignored():
    chunks = plan_chunks(1000, [Silence(5, 6)], 600, 600)
    assert chunks[0].cut_type == "forced"


def test_max_chunk_respects_size_limit():
    # 24 MB file limit at 128 kB/s -> ~168 s, below the 600 s ceiling
    limit = max_chunk_seconds(600, 24 * 1024 * 1024, 128 * 1024)
    assert limit < 600 and limit >= MIN_CHUNK_SECONDS


def test_stitch_offsets_timestamps_without_overlap():
    a = ChunkTranscript(0, "silence", 0, (AsrSegment(0, 5, "hello there"),))
    b = ChunkTranscript(541.5, "end", 0, (AsrSegment(0, 4, "next part"),))
    out = stitch([a, b])
    assert [(s.start, s.text) for s in out] == [(0, "hello there"), (541.5, "next part")]


def test_stitch_drops_duplicated_words_in_forced_cut_overlap():
    a = ChunkTranscript(0, "forced", 0, (AsrSegment(595, 600, "we need login"),))
    b = ChunkTranscript(
        598, "end", 2,
        (AsrSegment(0, 1.5, "we need login"), AsrSegment(1.6, 6, "and also logout")),
    )
    out = stitch([a, b])
    assert [s.text for s in out] == ["we need login", "and also logout"]


def test_quality_flags_hallucinated_silence_and_low_confidence():
    segs = [
        AsrSegment(0, 2, "real speech", -0.2, 0.01),
        AsrSegment(2, 4, "Thanks for watching!", -1.5, 0.9),
    ]
    usable, flagged, q = assess_quality(segs, 0.6, -1.0, 0.6)
    assert len(usable) == 1 and len(flagged) == 1 and q == TranscriptQuality.ok
    _, _, q2 = assess_quality(segs, 0.6, -1.0, 0.3)
    assert q2 == TranscriptQuality.low_confidence
    _, _, q3 = assess_quality([segs[1]], 0.6, -1.0, 0.3)
    assert q3 == TranscriptQuality.no_speech
