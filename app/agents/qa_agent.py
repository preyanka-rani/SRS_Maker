"""Blocking QA on a rendered SRS. Deterministic: the LLM never decides whether a document ships."""
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.srs_engine.context_builder import SrsContext

_UNRENDERED = re.compile(r"\{\{|\}\}|\{%|%\}")
_XML_TEXT = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
_EXTERNAL = re.compile(r'TargetMode="External"')
_GROUPS = ("frs", "nfrs", "secs", "rules", "ints", "others")


@dataclass(frozen=True)
class QaResult:
    passed: bool
    checks: dict
    problems: tuple[str, ...]

    def as_report(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "problems": list(self.problems)}


def _denylist(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return [ln.strip().lower() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def run_qa(docx_path: Path, pdf_pages: int, context: SrsContext, denylist_path: str) -> QaResult:
    problems: list[str] = []
    checks: dict = {}
    with zipfile.ZipFile(docx_path) as z:
        names = z.namelist()
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8", errors="ignore") \
            if "word/_rels/document.xml.rels" in names else ""
        everything = " ".join(z.read(n).decode("utf-8", errors="ignore")
                              for n in names if n.endswith((".xml", ".rels")))
    text = " ".join(_XML_TEXT.findall(xml))

    checks["no_unrendered_tags"] = not _UNRENDERED.search(text)
    if not checks["no_unrendered_tags"]:
        problems.append("Unrendered template tags remain in the document")

    codes = [r["code"] for key in _GROUPS for r in context.data[key]]
    missing = [c for c in codes if c not in text]
    checks["all_requirements_rendered"] = not missing
    if missing:
        problems.append("Requirements missing from the document: " + ", ".join(missing[:10]))

    checks["no_duplicate_requirements"] = len(codes) == len(set(codes))
    if not checks["no_duplicate_requirements"]:
        problems.append("A requirement appears more than once in the context")

    leaked = [w for w in _denylist(denylist_path) if w in everything.lower()]
    checks["no_leakage"] = not leaked
    if leaked:
        problems.append("Denylisted content found: " + ", ".join(leaked))

    checks["no_external_links"] = not _EXTERNAL.search(rels)
    if not checks["no_external_links"]:
        problems.append("External hyperlinks found in the document")

    version = context.data["doc"]["version"]
    last = context.data["revisions"][-1]["version"] if context.data["revisions"] else version
    checks["version_matches_history"] = version == last
    if version != last:
        problems.append("Document Control version differs from the last Revision History row")

    checks["pdf_has_pages"] = pdf_pages > 0
    if pdf_pages <= 0:
        problems.append("PDF has no pages")
    return QaResult(not problems, checks, tuple(problems))
