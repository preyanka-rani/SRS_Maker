"""Approval decision rules. Pure code: the model only supplies signals, never the verdict.

Eight checks, all required. Outcome precedence:
  refused  (definite failure: unauthorised sender, failed authentication, stale version)
  revision (the email also asks for changes / questions / has attachments: treat as feedback)
  manual   (anything uncertain)
  accepted (all eight pass)
"""
from dataclasses import dataclass, field

ACCEPTED, REFUSED, REVISION, MANUAL = "accepted", "refused", "revision", "manual_review"


@dataclass(frozen=True)
class ApprovalFacts:
    sender_is_approver: bool
    sender_auth: str                 # pass | fail | unknown
    require_sender_auth: bool
    binding: str                     # headers | subject_token_sender | none
    version_label_matches: bool      # label named in the email (or implied by headers) == current
    version_is_current: bool         # referenced version is current_srs_version and `sent`
    explicit_approval: bool
    approval_covers_all: bool
    signal_confidence: float
    min_confidence: float
    has_change_requests: bool
    has_questions: bool
    has_attachments: bool
    open_blockers: int
    integrity_ok: bool


@dataclass(frozen=True)
class ApprovalDecision:
    outcome: str
    checks: dict
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate(f: ApprovalFacts) -> ApprovalDecision:
    auth_ok = f.sender_auth == "pass" or (f.sender_auth == "unknown" and not f.require_sender_auth)
    binding_ok = f.binding == "headers" or (f.binding == "subject_token_sender" and f.version_label_matches)
    signal_ok = f.explicit_approval and f.approval_covers_all and f.signal_confidence >= f.min_confidence
    clean = not (f.has_change_requests or f.has_questions or f.has_attachments)
    checks = {
        "1_sender_authorized": f.sender_is_approver,
        "2_sender_authenticated": auth_ok,
        "3_thread_bound": binding_ok,
        "4_version_current": f.version_is_current,
        "5_explicit_approval": signal_ok,
        "6_nothing_else": clean,
        "7_no_open_blockers": f.open_blockers == 0,
        "8_integrity": f.integrity_ok,
    }
    refused = []
    if not f.sender_is_approver:
        refused.append("The sender is not an authorised approver for this project.")
    if f.sender_auth == "fail":
        refused.append("The sender could not be authenticated.")
    if not f.version_is_current:
        refused.append("The approval refers to a version that is no longer the current one.")
    if refused:
        return ApprovalDecision(REFUSED, checks, tuple(refused))
    if f.explicit_approval and not clean:
        return ApprovalDecision(REVISION, checks, ("The approval came with changes or questions.",))
    manual = [name for name, ok in checks.items() if not ok]
    if manual:
        return ApprovalDecision(MANUAL, checks, tuple(f"Check failed: {n}" for n in manual))
    return ApprovalDecision(ACCEPTED, checks)
