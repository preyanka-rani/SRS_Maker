"""Real ffmpeg: silence detection and silence-based chunking on synthetic audio."""
from pathlib import Path

import pytest

from app.integrations.transcription.base import TranscriptionResult
from app.processors.audio import ffmpeg_tools as ff
from app.processors.audio.pipeline import AudioConfig, transcribe_file
from app.processors.audio.transcript import AsrSegment
from tests.helpers import make_audio

# 40 s tone, 3 s silence, 40 s tone, 3 s silence, 14 s tone  (total 100 s)
PLAN = [("tone", 40), ("silence", 3), ("tone", 40), ("silence", 3), ("tone", 14)]


class RecordingProvider:
    """Returns one segment per chunk and records each chunk's real duration."""

    model_id = "recording-fake"

    def __init__(self):
        self.durations: list[float] = []
        self.prompts: list[str | None] = []

    def transcribe(self, path: Path, language=None, prompt=None) -> TranscriptionResult:
        duration = ff.probe(path).duration_seconds
        self.durations.append(duration)
        self.prompts.append(prompt)
        text = f"chunk {len(self.durations)} words"
        return TranscriptionResult((AsrSegment(0.0, min(duration, 5.0), text, -0.2, 0.01),),
                                   "en", duration)


@pytest.fixture(scope="module")
def tone_file(tmp_path_factory):
    return make_audio(tmp_path_factory.mktemp("audio") / "speech.wav", PLAN)


def test_probe_reads_duration_and_rejects_non_audio(tone_file, tmp_path):
    assert ff.probe(tone_file).duration_seconds == pytest.approx(100, abs=0.5)
    bogus = tmp_path / "x.mp3"
    bogus.write_bytes(b"this is not audio at all")
    with pytest.raises(ff.AudioError):
        ff.probe(bogus)


def test_silence_detection_finds_the_real_pauses(tone_file):
    silences = ff.detect_silences(tone_file, -35, 0.4, 100)
    spans = [(round(s.start), round(s.end)) for s in silences]
    assert (40, 43) in spans and (83, 86) in spans


def test_short_audio_is_sent_whole(tmp_path):
    f = make_audio(tmp_path / "short.wav", [("tone", 20)])
    provider = RecordingProvider()
    out = transcribe_file(f, provider, AudioConfig(chunk_max_seconds=600), workdir=tmp_path)
    assert out.chunk_count == 1 and out.cut_types == ("end",)


def test_cuts_land_inside_silence_and_chunks_stay_under_ceiling(tone_file, tmp_path):
    provider = RecordingProvider()
    cfg = AudioConfig(chunk_max_seconds=60, silence_search_window=120)
    out = transcribe_file(tone_file, provider, cfg, workdir=tmp_path)
    assert out.chunk_count == 2
    assert out.cut_types == ("silence", "end")
    # first chunk ends at the midpoint of the 40-43 s pause (41.5 s)
    assert provider.durations[0] == pytest.approx(41.5, abs=0.6)
    assert sum(provider.durations) == pytest.approx(100, abs=1.5)
    assert all(d <= 60 for d in provider.durations)
    # the second chunk receives the tail of the first transcript as context
    assert provider.prompts[0] is None and "chunk 1" in provider.prompts[1]
    # absolute timestamps: the second chunk's segment is offset by the cut point
    assert out.segments[1].start == pytest.approx(41.5, abs=0.6)


def test_size_limit_forces_more_chunks_than_the_time_ceiling(tone_file, tmp_path):
    provider = RecordingProvider()
    norm = tmp_path / "ref.flac"  # the pipeline sizes chunks from the normalized FLAC
    ff.normalize(tone_file, norm)
    cfg = AudioConfig(chunk_max_seconds=600, max_file_mb=(norm.stat().st_size / 1024 / 1024) / 2.5)
    out = transcribe_file(tone_file, provider, cfg, workdir=tmp_path)
    assert out.chunk_count >= 3  # a time-only ceiling of 600 s would have sent it whole


def test_audio_longer_than_limit_is_rejected(tone_file, tmp_path):
    with pytest.raises(ff.AudioError) as exc:
        transcribe_file(tone_file, RecordingProvider(), AudioConfig(max_minutes=1), workdir=tmp_path)
    assert exc.value.code == "audio_too_long"
