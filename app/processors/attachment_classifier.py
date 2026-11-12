"""Classify any attachment into a processing class. Pure, no I/O."""
from dataclasses import dataclass
from pathlib import PurePath

from app.database.models.sources import ProcessingClass

_TEXT = {".pdf", ".docx", ".txt"}
_AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".amr", ".3gp", ".flac",
          ".wma", ".weba"}
_VIDEO = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"}
_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".webp"}
_OFFICE = {".doc", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".csv",
           ".md", ".html", ".htm"}
_ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2"}
_EMAIL = {".eml", ".msg"}
_DANGEROUS = {".exe", ".dll", ".js", ".vbs", ".vbe", ".scr", ".bat", ".cmd", ".com", ".msi",
              ".ps1", ".jar", ".lnk", ".hta", ".docm", ".xlsm", ".pptm", ".dotm", ".xlsb"}

_MIME_PREFIX = {"audio/": ProcessingClass.audio, "video/": ProcessingClass.video,
                "image/": ProcessingClass.image}


@dataclass(frozen=True)
class AttachmentInfo:
    filename: str | None
    mime: str | None = None
    is_inline: bool = False
    size_bytes: int = 0


def classify(info: AttachmentInfo) -> ProcessingClass:
    ext = PurePath(info.filename or "").suffix.lower()
    mime = (info.mime or "").lower()

    if ext in _DANGEROUS:
        return ProcessingClass.dangerous
    # A tiny inline (cid:) image is a signature/logo, not a deliverable.
    if info.is_inline and (mime.startswith("image/") or ext in _IMAGE):
        return ProcessingClass.inline_decoration
    if mime == "message/rfc822" or ext in _EMAIL:
        return ProcessingClass.forwarded_email
    if ext in _TEXT:
        return ProcessingClass.text_supported
    if ext in _AUDIO:
        return ProcessingClass.audio
    if ext in _VIDEO:
        # webm/ogg can be audio-only; trust an explicit audio mime type over the extension.
        return ProcessingClass.audio if mime.startswith("audio/") else ProcessingClass.video
    if ext in _IMAGE:
        return ProcessingClass.image
    if ext in _ARCHIVE:
        return ProcessingClass.archive
    if ext in _OFFICE:
        return ProcessingClass.office_other
    for prefix, cls in _MIME_PREFIX.items():
        if mime.startswith(prefix):
            return cls
    if mime in {"application/pdf"}:
        return ProcessingClass.text_supported
    if mime in {"text/plain"}:
        return ProcessingClass.text_supported
    return ProcessingClass.unknown
