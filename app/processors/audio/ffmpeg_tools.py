"""Thin ffmpeg wrapper: probe, normalize to 16 kHz mono FLAC, detect silences, cut chunks.

ffprobe may be absent (e.g. imageio-ffmpeg ships ffmpeg only), so probing parses
`ffmpeg -i` output. All calls use a timeout and never go through a shell.
"""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.processors.audio.planner import Silence, parse_silencedetect

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_HAS_AUDIO = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:")
DEFAULT_TIMEOUT = 600


class AudioError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def ffmpeg_binary() -> str:
    env = os.environ.get("FFMPEG_BIN")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise AudioError("ffmpeg_missing", "ffmpeg is not installed") from exc


def _run(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - fixed binary, list args, no shell
            [ffmpeg_binary(), "-hide_banner", "-nostdin", *args],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg_timeout", "ffmpeg timed out") from exc


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float


def probe(path: Path) -> ProbeResult:
    proc = _run(["-i", str(path)], timeout=60)
    text = proc.stderr
    if not _HAS_AUDIO.search(text):
        raise AudioError("no_audio_stream", "File has no decodable audio stream")
    m = _DURATION.search(text)
    if not m:
        raise AudioError("unknown_duration", "Could not determine audio duration")
    h, mnt, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return ProbeResult(h * 3600 + mnt * 60 + sec)


def normalize(src: Path, dst: Path) -> None:
    """16 kHz mono FLAC: what Whisper uses internally, so nothing useful is lost."""
    proc = _run(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(dst)])
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("normalize_failed", "Audio could not be decoded")


def detect_silences(path: Path, noise_db: float, min_seconds: float, total: float,
                    start: float | None = None, end: float | None = None) -> list[Silence]:
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    args += ["-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}",
             "-f", "null", "-"]
    proc = _run(args)
    found = parse_silencedetect(proc.stderr, (end if end is not None else total) - (start or 0.0))
    offset = start or 0.0
    return [Silence(s.start + offset, s.end + offset) for s in found]


def cut(src: Path, dst: Path, start: float, end: float) -> None:
    proc = _run(["-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
                 "-c:a", "flac", str(dst)])
    if proc.returncode != 0 or not dst.exists():
        raise AudioError("cut_failed", "Could not extract audio chunk")
