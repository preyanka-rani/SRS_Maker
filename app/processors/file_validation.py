"""Validate uploaded/attached files before any parser touches them."""
import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000


class FileRejected(Exception):
    """Raised when a file fails validation. `code` is stable for issues/logging."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedFile:
    sha256: str
    size_bytes: int
    detected_kind: str  # "pdf" | "docx" | "txt"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_docx_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise FileRejected("zip_too_many_entries", "DOCX has too many entries")
            if sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
                raise FileRejected("zip_bomb", "DOCX expands beyond the allowed size")
            for info in infos:
                name = info.filename
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    raise FileRejected("zip_path_traversal", "DOCX contains an unsafe path")
            if "word/document.xml" not in zf.namelist():
                raise FileRejected("not_a_docx", "File is a zip but not a Word document")
    except zipfile.BadZipFile as exc:
        raise FileRejected("corrupt_docx", "DOCX is corrupt") from exc


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    for enc in ("utf-8", "utf-16"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def validate_document(path: Path, max_mb: int) -> ValidatedFile:
    """Content-sniff (never trust the extension alone) and apply size/safety limits."""
    size = path.stat().st_size
    if size == 0:
        raise FileRejected("empty_file", "File is empty")
    if size > max_mb * 1024 * 1024:
        raise FileRejected("too_large", f"File exceeds {max_mb} MB")
    with path.open("rb") as fh:
        head = fh.read(8192)
    ext = path.suffix.lower()
    if head.startswith(_PDF_MAGIC):
        kind = "pdf"
    elif head.startswith(_ZIP_MAGIC):
        _check_docx_zip(path)
        kind = "docx"
    elif _looks_like_text(head):
        kind = "txt"
    else:
        raise FileRejected("unsupported_type", "Unrecognised or binary file content")
    expected = {"pdf": ".pdf", "docx": ".docx", "txt": ".txt"}[kind]
    if ext != expected:
        raise FileRejected("extension_mismatch", f"Content is {kind} but extension is {ext!r}")
    return ValidatedFile(sha256=sha256_file(path), size_bytes=size, detected_kind=kind)
