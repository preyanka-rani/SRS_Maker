"""Trusted Authentication-Results parsing. Only headers stamped by OUR MX (authserv-id allowlist)
are believed; anything else is ignored, so a forged header can never prove identity."""
import re

from app.database.models import SenderAuth

_AUTHSERV = re.compile(r"^\s*([^;\s]+)")


def sender_verdict(headers: tuple[str, ...], trusted_ids: frozenset[str]) -> tuple[SenderAuth, int]:
    """Returns (verdict, number_of_untrusted_headers). Topmost trusted header wins."""
    untrusted = 0
    for header in headers:  # newest first, as stored by receiving MTAs
        m = _AUTHSERV.match(header)
        if not m or m.group(1).lower() not in trusted_ids:
            untrusted += 1
            continue
        lowered = header.lower()
        if "dmarc=pass" in lowered or "dkim=pass" in lowered:
            return SenderAuth.pass_, untrusted
        if "dmarc=fail" in lowered or "dkim=fail" in lowered or "spf=fail" in lowered:
            return SenderAuth.fail, untrusted
        return SenderAuth.unknown, untrusted
    return SenderAuth.unknown, untrusted
