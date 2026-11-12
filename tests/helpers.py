"""Test helpers: synthetic files (docx, minimal PDF, tone/silence audio) and DB shortcuts."""
import subprocess
import uuid
from pathlib import Path

import docx
from sqlalchemy.orm import Session

from app.database.models import Client, Project
from app.processors.audio.ffmpeg_tools import ffmpeg_binary


def make_project(db: Session, code: str | None = None, token: str | None = None) -> Project:
    code = code or f"P-{uuid.uuid4().hex[:6]}"
    client = Client(organisation_name=f"Client {code}")
    db.add(client)
    db.flush()
    project = Project(client_id=client.id, code=code, title="Northwind Portal",
                      project_token=token or f"PRJ-{uuid.uuid4().hex[:6].upper()}")
    db.add(project)
    db.flush()
    return project


def docx_bytes(tmp_path: Path, paragraphs: list[str], name: str = "r.docx") -> bytes:
    path = tmp_path / name
    document = docx.Document()
    for p in paragraphs:
        document.add_paragraph(p)
    document.save(path)
    return path.read_bytes()


def minimal_pdf(pages: list[str]) -> bytes:
    """Hand-built valid PDF; an empty string yields a page with no text (scanned-like)."""
    objects: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_obj = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
             f"/Resources << /Font << /F1 {font_obj} 0 R >> >> >>").encode())
        stream = (f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b"")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def make_audio(path: Path, plan: list[tuple[str, float]], rate: int = 16000) -> Path:
    """plan: [("tone", seconds) | ("silence", seconds), ...] -> audio file at `path`."""
    inputs: list[str] = []
    labels = ""
    for i, (kind, seconds) in enumerate(plan):
        src = (f"sine=frequency={300 + 100 * i}:sample_rate={rate}:duration={seconds}"
               if kind == "tone" else f"anullsrc=r={rate}:cl=mono")
        inputs += ["-f", "lavfi"] + (["-t", str(seconds)] if kind == "silence" else []) + ["-i", src]
        labels += f"[{i}:a]"
    graph = f"{labels}concat=n={len(plan)}:v=0:a=1[out]"
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", str(rate), str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path
