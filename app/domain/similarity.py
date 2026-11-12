"""Cheap, deterministic prefilter for duplicate/conflict candidates (token Jaccard).

The LLM only adjudicates pairs that pass this filter, which bounds cost and keeps the
decision about *which* pairs to consider reproducible.
"""
import re

from app.domain.hashing import normalize_statement

_STOP = frozenset("a an the and or of to in on for with by is are be shall should must will can may "
                  "system user users that this it as at from".split())
_TOKEN = re.compile(r"\w+", re.UNICODE)


_SUFFIXES = ("ing", "ed", "es", "s")
_MIN_STEM = 4


def _stem(token: str) -> str:
    """Very light stemming so 'emailed'/'emails'/'email' compare equal (recall over precision)."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            return token[: -len(suffix)]
    return token


def tokens(text: str) -> frozenset[str]:
    return frozenset(_stem(t) for t in _TOKEN.findall(normalize_statement(text))
                     if t not in _STOP and len(t) > 1)


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def candidate_pairs(new_statement: str, existing: list[tuple[str, str]], threshold: float = 0.35,
                    limit: int = 5) -> list[tuple[str, float]]:
    """existing: [(code, statement)]. Returns the best (code, score) pairs above threshold."""
    scored = [(code, jaccard(new_statement, stmt)) for code, stmt in existing]
    scored = [(c, s) for c, s in scored if s >= threshold]
    return sorted(scored, key=lambda x: (-x[1], x[0]))[:limit]
