"""SMTP outbound adapter. Returns the server's reply code; certificate verification is on."""
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate

from app.core.config import Settings


class SmtpTemporaryError(Exception):
    """4xx / network failure: retry later."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class SmtpPermanentError(Exception):
    """5xx: do not retry."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OutgoingMail:
    message_id: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[tuple[str, str, bytes], ...] = ()  # (filename, mime, data)
    outbox_id: str | None = None


def build_message(settings: Settings, mail: OutgoingMail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f'"{settings.smtp_from_name}" <{settings.mail_from_address}>'
    msg["To"] = ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    msg["Subject"] = mail.subject
    msg["Message-ID"] = mail.message_id
    msg["Date"] = formatdate(localtime=False)
    msg["Auto-Submitted"] = "auto-generated"
    if mail.outbox_id:
        msg["X-SRS-Outbox-Id"] = mail.outbox_id
    if mail.in_reply_to:
        msg["In-Reply-To"] = mail.in_reply_to
    if mail.references:
        msg["References"] = " ".join(mail.references)
    msg.set_content(mail.body_text)
    if mail.body_html:
        msg.add_alternative(mail.body_html, subtype="html")
    for filename, mime, data in mail.attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=filename)
    return msg


class SmtpAdapter:
    def __init__(self, settings: Settings):
        if not (settings.smtp_host and settings.smtp_username and settings.smtp_password):
            raise RuntimeError("SMTP is not configured (SMTP_SERVER / SMTP_EMAIL / SMTP_PASSWORD)")
        self._s = settings

    def send(self, mail: OutgoingMail) -> int:
        s = self._s
        msg = build_message(s, mail)
        context = ssl.create_default_context()
        recipients = list(mail.to) + list(mail.cc)
        try:
            if s.smtp_security == "ssl" or s.smtp_port == 465:
                server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=60)
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60)
            with server:
                if s.smtp_security != "ssl" and s.smtp_port != 465:
                    server.starttls(context=context)
                server.login(s.smtp_username, s.smtp_password.get_secret_value())
                refused = server.send_message(msg, to_addrs=recipients)
        except smtplib.SMTPRecipientsRefused as exc:
            raise SmtpPermanentError("All recipients refused", 550) from exc
        except smtplib.SMTPResponseException as exc:
            cls = SmtpPermanentError if 500 <= exc.smtp_code < 600 else SmtpTemporaryError
            raise cls(f"SMTP {exc.smtp_code}", exc.smtp_code) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpTemporaryError(f"SMTP transport error: {type(exc).__name__}") from exc
        if refused and len(refused) >= len(recipients):
            raise SmtpPermanentError("All recipients refused", 550)
        return 250
