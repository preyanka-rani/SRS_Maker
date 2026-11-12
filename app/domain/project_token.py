"""Project token used in email subjects: [PRJ-XXXXXX]. Unambiguous alphabet, no I/O/0/1."""
import re
import secrets

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_LENGTH = 6
_TOKEN_RE = re.compile(r"^PRJ-[A-Z0-9]{6}$")
SUBJECT_TOKEN_RE = re.compile(r"\[(PRJ-[A-Z0-9]{6})\]")


def generate_project_token() -> str:
    return "PRJ-" + "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LENGTH))


def is_valid_project_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token))
