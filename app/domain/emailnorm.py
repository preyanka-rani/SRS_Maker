"""Email address normalization. Addresses are stored lowercase (no citext extension needed)."""
from email.utils import parseaddr


def normalize_email(address: str) -> str:
    """Lowercase + strip; accepts 'Name <a@b.c>' forms. Returns '' if no address is found."""
    _, addr = parseaddr(address or "")
    return addr.strip().lower()


def email_domain(address: str) -> str:
    addr = normalize_email(address)
    return addr.rsplit("@", 1)[1] if "@" in addr else ""
