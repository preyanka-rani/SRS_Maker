"""PDF / DOCX / TXT text extraction into addressable segments.

PDF: pdfplumber (MIT) by default, pypdfium2 (Apache/BSD) as fallback. PyMuPDF (AGPL) is
deliberately NOT used.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.processors.unicode_sanitizer import sanitize_text

# Pages with fewer extracted characters than this are treated as scanned/image-only.
MIN_CHARS_PER_PAGE = 10
NEEDS_OCR_PAGE_RATIO = 0.5


class ExtractionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Segment:
    text: str
    page: int | None = None
    paragraph: int | None = None


@dataclass(frozen=True)
class ExtractedDocument:
    segments: tuple[Segment, ...]
    page_count: int | None
    needs_ocr: bool
    flags: tuple[str, ...] = ()

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


def _clean_segments(raw: list[Segment]) -> tuple[list[Segment], set[str]]:
    flags: set[str] = set()
    out: list[Segment] = []
    for seg in raw:
        text, f = sanitize_text(seg.text)
        flags.update(f)
        text = text.strip()
        if text:
            out.append(Segment(text, seg.page, seg.paragraph))
    return out, flags


class PdfExtractor(Protocol):
    def extract(self, path: Path) -> tuple[list[Segment], int]: ...


class PdfPlumberExtractor:
    def extract(self, path: Path) -> tuple[list[Segment], int]:
        import pdfplumber

        segments: list[Segment] = []
        with pdfplumber.open(path) as pdf:
            if getattr(pdf, "doc", None) is not None and pdf.doc.is_extractable is False:
                raise ExtractionError("encrypted_pdf", "PDF does not allow text extraction")
            for page_no, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                for para_no, para in enumerate(_split_paragraphs(text), start=1):
                    segments.append(Segment(para, page_no, para_no))
            return segments, len(pdf.pages)


class PdfiumExtractor:
    def extract(self, path: Path) -> tuple[list[Segment], int]:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            segments: list[Segment] = []
            for page_no in range(len(pdf)):
                page = pdf[page_no]
                text = page.get_textpage().get_text_range()
                for para_no, para in enumerate(_split_paragraphs(text), start=1):
                    segments.append(Segment(para, page_no + 1, para_no))
            return segments, len(pdf)
        finally:
            pdf.close()


def _split_paragraphs(text: str) -> list[str]:
    blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n")]
    paras = [" ".join(b.split()) for b in blocks if b.strip()]
    if len(paras) <= 1 and text.count("\n") > 3:  # single block with hard line breaks
        paras = [" ".join(line.split()) for line in text.split("\n") if line.strip()]
    return paras


def extract_pdf(path: Path, primary: PdfExtractor | None = None,
                fallback: PdfExtractor | None = None) -> ExtractedDocument:
    primary = primary or PdfPlumberExtractor()
    fallback = fallback or PdfiumExtractor()
    try:
        segments, pages = primary.extract(path)
    except ExtractionError:
        raise
    except Exception:  # noqa: BLE001 - any pdfplumber failure triggers the fallback
        try:
            segments, pages = fallback.extract(path)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError("corrupt_pdf", "PDF could not be parsed") from exc
    cleaned, flags = _clean_segments(segments)
    per_page = {}
    for seg in cleaned:
        per_page[seg.page] = per_page.get(seg.page, 0) + len(seg.text)
    sparse = sum(1 for p in range(1, pages + 1) if per_page.get(p, 0) < MIN_CHARS_PER_PAGE)
    needs_ocr = pages > 0 and sparse / pages >= NEEDS_OCR_PAGE_RATIO
    return ExtractedDocument(tuple(cleaned), pages, needs_ocr, tuple(sorted(flags)))


def extract_docx(path: Path) -> ExtractedDocument:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("corrupt_docx", "DOCX could not be parsed") from exc
    raw: list[Segment] = []
    para_no = 0
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            text = "\n".join(
                " | ".join(cell.text.strip() for cell in row.cells) for row in table.rows
            )
        else:
            continue
        para_no += 1
        raw.append(Segment(text, None, para_no))
    cleaned, flags = _clean_segments(raw)
    return ExtractedDocument(tuple(cleaned), None, False, tuple(sorted(flags)))


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def extract_txt(path: Path) -> ExtractedDocument:
    text = decode_text(path.read_bytes())
    raw = [Segment(p, None, i) for i, p in enumerate(_split_paragraphs(text), start=1)]
    cleaned, flags = _clean_segments(raw)
    return ExtractedDocument(tuple(cleaned), None, False, tuple(sorted(flags)))


def extract(path: Path, kind: str) -> ExtractedDocument:
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "docx":
        return extract_docx(path)
    if kind == "txt":
        return extract_txt(path)
    raise ExtractionError("unsupported_type", f"No extractor for {kind}")
