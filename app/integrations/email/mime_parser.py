"""Parse a raw RFC 822 message into a plain, immutable structure. Never raises on odd mail."""
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from datetime import UTC, datetime

from app.domain.emailnorm import normalize_email
from app.processors.unicode_sanitizer import sanitize_text

_MSGID = re.compile(r"<[^<>\s]+>")
_QUOTE_HEADERS = re.compile(
    r"^(on .{5,200} wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+|sent from my .+|"
    r"_{5,}|-{5,}\s*forwarded message\s*-{5,})\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str | None
    content_type: str
    data: bytes
    is_inline: bool
    content_id: str | None = None


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    from_addresses: tuple[str, ...]
    reply_to: tuple[str, ...]
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    date: datetime | None
    body_text: str
    new_content_text: str
    attachments: tuple[ParsedAttachment, ...] = field(default_factory=tuple)
    is_auto_generated: bool = False
    is_dsn: bool = False
    authentication_results: tuple[str, ...] = ()
    delivery_status: str | None = None
    is_bulk: bool = False


def _addresses(msg: EmailMessage, header: str) -> tuple[str, ...]:
    values = [str(v) for v in msg.get_all(header, [])]
    out = [normalize_email(a) for _, a in getaddresses(values) if a and "@" in a]
    return tuple(dict.fromkeys(out))


def strip_quoted_reply(text: str) -> str:
    """Keep only the newly written part of a reply (drop quotes, signatures, headers)."""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_HEADERS.match(stripped) or stripped == "--":
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(a, b)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("html",))
    return _html_to_text(part.get_content()) if part is not None else ""


def parse_email(raw: bytes, fallback_id: str | None = None) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    ids = _MSGID.findall(str(msg.get("Message-ID", "")))
    message_id = ids[0] if ids else (fallback_id or f"<missing-{abs(hash(raw))}@local>")
    irt = _MSGID.findall(str(msg.get("In-Reply-To", "")))
    refs = tuple(_MSGID.findall(" ".join(str(h) for h in msg.get_all("References", []))))
    attachments: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        filename = part.get_filename()
        if ctype == "message/rfc822":
            inner = part.get_payload(0)
            data = inner.as_bytes() if inner is not None else b""
            attachments.append(ParsedAttachment(filename or "forwarded.eml", ctype, data, False))
            continue
        if disposition == "attachment" or filename or (disposition == "inline"
                                                       and not ctype.startswith("text/")):
            data = part.get_payload(decode=True) or b""
            attachments.append(ParsedAttachment(
                filename, ctype, data, disposition == "inline" and not filename,
                str(part.get("Content-ID") or "").strip("<>") or None))
    ctype_top = msg.get_content_type()
    is_dsn = ctype_top == "multipart/report" and "delivery-status" in str(msg.get_content_type()) + \
        str(msg.get_param("report-type") or "")
    auto = str(msg.get("Auto-Submitted", "no")).lower() != "no" or \
        str(msg.get("Precedence", "")).lower() in {"bulk", "junk", "auto_reply"} or \
        bool(msg.get("X-Autoreply") or msg.get("X-Autorespond"))
    date = None
    try:
        if msg.get("Date"):
            date = parsedate_to_datetime(str(msg["Date"]))
            date = date.astimezone(UTC).replace(tzinfo=None) if date.tzinfo else date
    except (TypeError, ValueError):
        date = None
    body = sanitize_text(_body(msg))[0]
    return ParsedEmail(
        message_id=message_id, in_reply_to=irt[0] if irt else None, references=refs,
        from_addresses=_addresses(msg, "From"), reply_to=_addresses(msg, "Reply-To"),
        to=_addresses(msg, "To"), cc=_addresses(msg, "Cc"),
        subject=sanitize_text(str(msg.get("Subject", "")))[0].replace("\n", " ").strip(),
        date=date, body_text=body, new_content_text=strip_quoted_reply(body),
        attachments=tuple(attachments), is_auto_generated=auto, is_dsn=is_dsn,
        authentication_results=tuple(str(v) for v in msg.get_all("Authentication-Results", [])),
        delivery_status=None,
        is_bulk=bool(msg.get("List-Unsubscribe") or msg.get("List-Id")
                     or str(msg.get("Precedence", "")).lower() in {"bulk", "list", "junk"}))
