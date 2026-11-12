import zipfile
from pathlib import Path

import docx
import pytest

from app.database.models.sources import ProcessingClass as PC
from app.processors.attachment_classifier import AttachmentInfo, classify
from app.processors.chunking import build_chunks
from app.processors.extractors import extract_docx, extract_txt
from app.processors.file_validation import FileRejected, validate_document
from app.processors.unicode_sanitizer import sanitize_text, strip_xml_invalid


# ---- unicode sanitizer ----
def test_sanitizer_strips_bidi_and_zero_width_but_keeps_zwj_zwnj():
    text = "ab‮c​d⁦e‌f‍g﻿h"
    clean, flags = sanitize_text(text)
    assert clean == "abcde‌f‍gh"
    assert "bidi_or_invisible" in flags


def test_sanitizer_keeps_bangla_conjuncts_intact():
    bangla = "ক্ষমা‌কর"
    clean, _ = sanitize_text(bangla)
    assert clean == bangla


def test_sanitizer_normalizes_crlf_and_drops_control_chars():
    clean, flags = sanitize_text("a\r\nb\x07c")
    assert clean == "a\nbc"
    assert "control_chars" in flags


def test_strip_xml_invalid():
    assert strip_xml_invalid("a\x00b\x0bc퟿d") == "abc퟿d"


# ---- attachment classifier ----
@pytest.mark.parametrize(
    "name,mime,inline,expected",
    [
        ("req.pdf", "application/pdf", False, PC.text_supported),
        ("notes.DOCX", None, False, PC.text_supported),
        ("voice.m4a", "audio/mp4", False, PC.audio),
        ("call.amr", None, False, PC.audio),
        ("clip.webm", "audio/webm", False, PC.audio),
        ("clip.webm", "video/webm", False, PC.video),
        ("demo.mp4", "video/mp4", False, PC.video),
        ("sheet.xlsx", None, False, PC.office_other),
        ("bundle.zip", "application/zip", False, PC.archive),
        ("fwd.eml", "message/rfc822", False, PC.forwarded_email),
        ("logo.png", "image/png", True, PC.inline_decoration),
        ("photo.png", "image/png", False, PC.image),
        ("run.exe", None, False, PC.dangerous),
        ("macro.docm", None, False, PC.dangerous),
        ("mystery.bin", "application/octet-stream", False, PC.unknown),
    ],
)
def test_classify(name, mime, inline, expected):
    assert classify(AttachmentInfo(name, mime, inline)) == expected


# ---- file validation ----
def _make_docx(path: Path, paragraphs: list[str]) -> None:
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(path)


def test_validate_accepts_docx_and_txt(tmp_path):
    f = tmp_path / "a.docx"
    _make_docx(f, ["hello"])
    assert validate_document(f, 5).detected_kind == "docx"
    t = tmp_path / "a.txt"
    t.write_text("plain text", encoding="utf-8")
    assert validate_document(t, 5).detected_kind == "txt"


def test_validate_rejects_empty_oversized_mismatch_and_binary(tmp_path):
    empty = tmp_path / "e.txt"
    empty.write_bytes(b"")
    with pytest.raises(FileRejected) as exc:
        validate_document(empty, 5)
    assert exc.value.code == "empty_file"

    big = tmp_path / "b.txt"
    big.write_bytes(b"a" * (2 * 1024 * 1024))
    with pytest.raises(FileRejected) as exc:
        validate_document(big, 1)
    assert exc.value.code == "too_large"

    mismatch = tmp_path / "m.pdf"
    mismatch.write_text("just text", encoding="utf-8")
    with pytest.raises(FileRejected) as exc:
        validate_document(mismatch, 5)
    assert exc.value.code == "extension_mismatch"

    binary = tmp_path / "x.txt"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe\x00")
    with pytest.raises(FileRejected) as exc:
        validate_document(binary, 5)
    assert exc.value.code == "unsupported_type"


def test_validate_rejects_corrupt_zip_and_path_traversal(tmp_path):
    corrupt = tmp_path / "c.docx"
    corrupt.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(FileRejected) as exc:
        validate_document(corrupt, 5)
    assert exc.value.code == "corrupt_docx"

    evil = tmp_path / "evil.docx"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("word/document.xml", "<x/>")
        zf.writestr("../escape.txt", "x")
    with pytest.raises(FileRejected) as exc:
        validate_document(evil, 5)
    assert exc.value.code == "zip_path_traversal"


# ---- extractors ----
def test_extract_docx_preserves_body_order_with_tables(tmp_path):
    f = tmp_path / "r.docx"
    d = docx.Document()
    d.add_paragraph("Intro paragraph")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "ID"
    table.rows[0].cells[1].text = "Req"
    d.add_paragraph("Closing paragraph")
    d.save(f)
    doc = extract_docx(f)
    assert [s.text for s in doc.segments] == ["Intro paragraph", "ID | Req", "Closing paragraph"]
    assert not doc.needs_ocr


def test_extract_txt_handles_utf8_bom_and_bangla(tmp_path):
    f = tmp_path / "b.txt"
    f.write_bytes("﻿সিস্টেম লগইন\n\nSecond para".encode("utf-8"))
    doc = extract_txt(f)
    assert doc.segments[0].text == "সিস্টেম লগইন"
    assert len(doc.segments) == 2


# ---- chunking ----
def test_chunking_never_splits_items_and_isolates_oversized():
    items = [("a", "x" * 40), ("b", "y" * 40), ("c", "z" * 500), ("d", "w" * 10)]
    chunks = build_chunks(items, max_chars=100)
    assert [c.evidence_ids for c in chunks] == [("a", "b"), ("c",), ("d",)]
    assert [c.index for c in chunks] == [0, 1, 2]
