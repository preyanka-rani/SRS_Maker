import pytest

from app.core.errors import ValidationFailed
from app.database.models.requirements import ReqStatus as S
from app.database.models.requirements import ReqType
from app.domain.hashing import (
    input_fingerprint,
    normalize_statement,
    requirement_set_hash,
    statement_hash,
    substantive_hash,
)
from app.domain.lifecycle import can_transition, format_code, id_prefix, require_transition
from app.domain.similarity import candidate_pairs, jaccard


def _req(code="FR-001", status="proposed", rev=1, statement="The system shall log users in."):
    return {"code": code, "req_type": "FR", "title": "Login", "statement": statement,
            "content_revision": rev, "status": status, "history_seq": rev, "details": {},
            "acceptance_criteria": [], "notification_specs": [], "evidence_link_ids": ["e1"]}


# ---- hashing ----
def test_status_change_does_not_alter_substantive_hash_but_alters_set_hash():
    a, b = [_req(status="proposed")], [_req(status="confirmed")]
    assert substantive_hash(a, {}, {}) == substantive_hash(b, {}, {})
    assert requirement_set_hash(a, {}, {}) != requirement_set_hash(b, {}, {})


def test_content_change_alters_substantive_hash():
    assert substantive_hash([_req()], {}, {}) != substantive_hash(
        [_req(statement="The system shall log users out.")], {}, {})
    assert substantive_hash([_req(rev=1)], {}, {}) != substantive_hash([_req(rev=2)], {}, {})


def test_deprecated_requirements_are_excluded_from_substantive_hash():
    live = [_req("FR-001")]
    with_dead = live + [_req("FR-002", status="deprecated")]
    assert substantive_hash(live, {}, {}) == substantive_hash(with_dead, {}, {})
    assert requirement_set_hash(live, {}, {}) != requirement_set_hash(with_dead, {}, {})


def test_hash_is_order_independent_and_unicode_normalized():
    a = [_req("FR-001"), _req("FR-002")]
    assert substantive_hash(a, {}, {}) == substantive_hash(list(reversed(a)), {}, {})
    composed, decomposed = "é", "é"
    assert substantive_hash([_req(statement=composed)], {}, {}) == \
        substantive_hash([_req(statement=decomposed)], {}, {})


def test_artifacts_and_metadata_affect_hash():
    base = substantive_hash([_req()], {"modules": []}, {"title": "A"})
    assert base != substantive_hash([_req()], {"modules": ["M1"]}, {"title": "A"})
    assert base != substantive_hash([_req()], {"modules": []}, {"title": "B"})


def test_statement_normalization_for_duplicates():
    assert statement_hash("The System SHALL log in!") == statement_hash("the system shall   log in")
    assert normalize_statement("A,  B.") == "a b"


def test_input_fingerprint_changes_with_template_or_prompts():
    f = input_fingerprint("s", "i", 1, {"p": "1"})
    assert f != input_fingerprint("s", "i", 2, {"p": "1"})
    assert f != input_fingerprint("s", "i", 1, {"p": "2"})
    assert f == input_fingerprint("s", "i", 1, {"p": "1"})


# ---- lifecycle ----
@pytest.mark.parametrize("old,new,ok", [
    (S.proposed, S.confirmed, True), (S.proposed, S.needs_clarification, True),
    (S.confirmed, S.modified, True), (S.modified, S.confirmed, True),
    (S.confirmed, S.deprecated, True), (S.deprecated, S.proposed, True),
    (S.confirmed, S.proposed, False), (S.deprecated, S.confirmed, False),
    (S.needs_clarification, S.confirmed, False),
])
def test_transitions(old, new, ok):
    assert can_transition(old, new) is ok
    if not ok:
        with pytest.raises(ValidationFailed):
            require_transition(old, new)


def test_id_prefixes_follow_template_conventions():
    assert id_prefix(ReqType.FR) == "FR"
    assert id_prefix(ReqType.NFR, "perf") == "NFR-PERF"
    assert id_prefix(ReqType.NFR, None) == "NFR-GEN"
    assert id_prefix(ReqType.BUS_RULE) == "BUS-RULE"
    assert format_code("FR", 7) == "FR-007"
    assert format_code("NFR-PERF", 12) == "NFR-PERF-012"


# ---- similarity ----
def test_paraphrased_duplicates_score_high_and_unrelated_low():
    a = "The system shall allow users to reset their password via email."
    b = "Users can reset their password using an email link."
    c = "The system shall export monthly invoices as PDF."
    assert jaccard(a, b) > 0.3 > jaccard(a, c)


def test_candidate_pairs_sorted_thresholded_and_limited():
    existing = [("FR-001", "Users reset password by email"), ("FR-002", "Export invoices to PDF"),
                ("FR-003", "Users reset their password by email link")]
    pairs = candidate_pairs("Users can reset password by email", existing, threshold=0.4)
    assert [c for c, _ in pairs][0] in {"FR-001", "FR-003"}
    assert "FR-002" not in [c for c, _ in pairs]
