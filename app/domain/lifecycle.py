"""Requirement lifecycle state machine and ID prefixes. Pure functions."""
from app.core.errors import ValidationFailed
from app.database.models.requirements import ReqStatus, ReqType

S = ReqStatus

_ALLOWED: dict[ReqStatus, frozenset[ReqStatus]] = {
    S.proposed: frozenset({S.needs_clarification, S.confirmed, S.deprecated, S.proposed}),
    S.needs_clarification: frozenset({S.proposed, S.deprecated, S.needs_clarification}),
    S.confirmed: frozenset({S.modified, S.deprecated}),
    S.modified: frozenset({S.confirmed, S.needs_clarification, S.deprecated, S.modified}),
    S.deprecated: frozenset({S.proposed}),  # restore (staff only)
}

_PREFIX = {
    ReqType.FR: "FR", ReqType.SEC: "SEC", ReqType.BUS_RULE: "BUS-RULE", ReqType.INT: "INT",
    ReqType.BR: "BR", ReqType.CON: "CON", ReqType.ASM: "ASM", ReqType.DEP: "DEP",
    ReqType.REPORT: "RPT", ReqType.UI_SCREEN: "UI", ReqType.SCOPE_EXCLUSION: "OOS",
    ReqType.FUTURE_SCOPE: "FUT", ReqType.TECH: "TECH",
}


def can_transition(old: ReqStatus, new: ReqStatus) -> bool:
    return new in _ALLOWED.get(old, frozenset())


def require_transition(old: ReqStatus, new: ReqStatus) -> None:
    if not can_transition(old, new):
        raise ValidationFailed(f"Illegal requirement status change {old.value} -> {new.value}")


def id_prefix(req_type: ReqType, category: str | None = None) -> str:
    """FR-001, NFR-PERF-001, SEC-001, BUS-RULE-001 ... (category only for NFR)."""
    if req_type == ReqType.NFR:
        cat = "".join(ch for ch in (category or "GEN").upper() if ch.isalnum())[:8] or "GEN"
        return f"NFR-{cat}"
    return _PREFIX[req_type]


def format_code(prefix: str, number: int) -> str:
    return f"{prefix}-{number:03d}"
