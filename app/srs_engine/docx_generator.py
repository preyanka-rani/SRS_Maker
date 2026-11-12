"""Render the Clean Base Template with docxtpl. Output is deterministic for a given context."""
import hashlib
from pathlib import Path

from docxtpl import DocxTemplate

from app.core.config import Settings
from app.srs_engine.context_builder import SrsContext

RENDERER_VERSION = "docxtpl-1"


def template_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_template(settings: Settings) -> Path:
    """The template is committed to the repo; it is (re)built from code only if it is missing."""
    template = Path(settings.template_path)
    if not template.exists():
        from app.srs_engine.template_builder import build_template, write_support_files

        build_template(template)
        write_support_files()
    return template


def render_docx(settings: Settings, context: SrsContext, out_path: Path) -> str:
    """Returns the sha256 of the written file."""
    doc = DocxTemplate(str(ensure_template(settings)))
    doc.render(context.data, autoescape=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return hashlib.sha256(out_path.read_bytes()).hexdigest()
