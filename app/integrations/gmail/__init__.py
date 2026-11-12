"""Gmail (IMAP/SMTP) adapters. Any IMAP/SMTP provider works; Gmail needs an app password."""
from app.integrations.email.imap_adapter import FetchedMail, ImapAdapter
from app.integrations.email.smtp_adapter import OutgoingMail, SmtpAdapter

__all__ = ["FetchedMail", "ImapAdapter", "OutgoingMail", "SmtpAdapter"]
