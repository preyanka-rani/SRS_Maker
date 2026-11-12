"""PDF text extraction: pdfplumber first, pypdfium2 as fallback (no AGPL dependency)."""
from app.processors.extractors import PdfPlumberExtractor, PdfiumExtractor, extract_pdf

__all__ = ["PdfPlumberExtractor", "PdfiumExtractor", "extract_pdf"]
