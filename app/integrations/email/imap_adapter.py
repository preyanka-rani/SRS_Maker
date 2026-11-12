"""IMAP inbound adapter. Uses BODY.PEEK (mark_seen=False): the system never changes mailbox state."""
from dataclasses import dataclass

from imap_tools import AND, MailBox, MailMessageFlags

from app.core.config import Settings


@dataclass(frozen=True)
class FetchedMail:
    uid: int
    uidvalidity: int | None
    raw: bytes


class ImapAdapter:
    def __init__(self, settings: Settings):
        if not (settings.imap_host and settings.imap_username and settings.imap_password):
            raise RuntimeError("IMAP is not configured (IMAP_SERVER / IMAP_EMAIL / IMAP_PASSWORD)")
        self._s = settings

    def fetch_after(self, last_uid: int, limit: int = 50) -> list[FetchedMail]:
        s = self._s
        with MailBox(s.imap_host, s.imap_port).login(
                s.imap_username, s.imap_password.get_secret_value(), initial_folder=s.imap_folder) as box:
            status = box.folder.status(s.imap_folder)
            validity = int(status.get("UIDVALIDITY", 0)) or None
            uid_range = f"{last_uid + 1}:*"
            criteria = (AND(seen=False, uid=uid_range) if s.imap_unseen_only
                        else AND(uid=uid_range))
            mails = box.fetch(criteria, mark_seen=False, bulk=False, limit=limit, reverse=False)
            return [FetchedMail(int(m.uid), validity, m.obj.as_bytes()) for m in mails
                    if int(m.uid) > last_uid]

    def mark_seen(self, uids: list[int]) -> None:
        """Mark the mails we have taken responsibility for as read (the others stay unread)."""
        if not uids:
            return
        s = self._s
        with MailBox(s.imap_host, s.imap_port).login(
                s.imap_username, s.imap_password.get_secret_value(),
                initial_folder=s.imap_folder) as box:
            box.flag([str(u) for u in uids], MailMessageFlags.SEEN, True)
