"""Canonical hashing (hash scheme v1). Pure functions.

Encoding: JSON with sorted keys, no whitespace, NFC-normalized text, SHA-256.

* substantive_hash  - MEANING of the document. Excludes lifecycle status and presentation, so
                      confirming requirements never changes it.
* requirement_set_hash - substantive inputs + each requirement's status + the deprecated set.
* content_hash      - the full rendered context (labels, statuses, document control).
"""
import hashlib
import json
import re
import unicodedata
from typing import Any

HASH_SCHEME_VERSION = 1
_WS = re.compile(r"\s+")


def nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {nfc(k): nfc(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [nfc(v) for v in value]
    return value


def canonical_json(obj: Any) -> str:
    return json.dumps(nfc(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def normalize_statement(text: str) -> str:
    """Case/whitespace/punctuation-insensitive form for exact-duplicate detection."""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return _WS.sub(" ", text).strip()


def statement_hash(text: str) -> str:
    return hashlib.sha256(normalize_statement(text).encode("utf-8")).hexdigest()


# fields of a requirement snapshot that carry meaning (status/history_seq excluded on purpose)
SUBSTANTIVE_FIELDS = ("code", "req_type", "category", "module", "title", "statement", "rationale",
                      "priority", "content_revision", "details", "acceptance_criteria",
                      "notification_specs", "evidence_link_ids")


def substantive_hash(requirements: list[dict], artifacts: dict, metadata: dict) -> str:
    """requirements: snapshot dicts (non-deprecated only are hashed)."""
    live = sorted((r for r in requirements if r.get("status") != "deprecated"),
                  key=lambda r: r["code"])
    body = [{k: r.get(k) for k in SUBSTANTIVE_FIELDS} for r in live]
    return sha256_json({"v": HASH_SCHEME_VERSION, "requirements": body,
                        "artifacts": artifacts, "metadata": metadata})


def requirement_set_hash(requirements: list[dict], artifacts: dict, metadata: dict) -> str:
    base = substantive_hash(requirements, artifacts, metadata)
    statuses = sorted((r["code"], r.get("status")) for r in requirements)
    return sha256_json({"v": HASH_SCHEME_VERSION, "substantive": base, "statuses": statuses})


def input_fingerprint(requirement_set: str, issues_hash: str, template_version: int,
                      prompt_versions: dict) -> str:
    return sha256_json({"v": HASH_SCHEME_VERSION, "set": requirement_set, "issues": issues_hash,
                        "template": template_version, "prompts": prompt_versions})
