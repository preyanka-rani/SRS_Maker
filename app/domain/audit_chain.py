"""Hash chaining for the append-only audit log (pure functions, no DB access)."""
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_entry_hash(prev_hash: str, entry: Mapping[str, Any]) -> str:
    payload = prev_hash + canonical_json(dict(entry))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_chain(entries: Iterable[Mapping[str, Any]]) -> bool:
    """Each entry must carry prev_hash, hash and the hashed fields under 'body'."""
    expected_prev = GENESIS_HASH
    for entry in entries:
        if entry["prev_hash"] != expected_prev:
            return False
        if compute_entry_hash(entry["prev_hash"], entry["body"]) != entry["hash"]:
            return False
        expected_prev = entry["hash"]
    return True
