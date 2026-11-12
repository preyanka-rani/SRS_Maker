"""validation_agent: dedupe against existing requirements, adjudicate duplicates/conflicts with the
model, raise ambiguity issues, and persist accepted candidates. Conflicts become blocking issues."""
import uuid

from sqlalchemy.orm import Session

from app.database.models import (
    CandidateDisposition,
    CauseType,
    EvidenceRelation,
    IssueType,
    ReqStatus,
    Requirement,
    RequirementCandidate,
    RequirementEvidence,
)
from app.agents.extraction_agent import ExtractionReport
from app.domain.hashing import statement_hash
from app.domain.similarity import candidate_pairs
from app.llm.output_schemas import AdjudicationOutput, RequirementCandidateDraft
from app.llm.prompt_registry import load_prompt
from app.services import issue_service
from app.services import requirement_service as rs
from app.services.llm_service import GuardedLlm

LOW_CONFIDENCE = 0.5
ADJUDICATE_PROMPT = "adjudicate_pairs"


def _draft_to_dict(payload: dict) -> dict:
    d = RequirementCandidateDraft.model_validate(
        {k: v for k, v in payload.items() if k != "evidence_ids"}).model_dump()
    d["confidence"] = payload.get("confidence")
    d["details"] = {"purpose": d.pop("purpose"), "actors": d.pop("actors"),
                    "trigger": d.pop("trigger"), "preconditions": d.pop("preconditions"),
                    "main_flow": d.pop("main_flow")}
    d["acceptance_criteria"] = [
        {"title": c["scenario_title"], "given": c["given"], "when": c["when"], "then": c["then"]}
        for c in d["acceptance_criteria"]]
    return d


def _link_confirming_evidence(session: Session, req: Requirement, evidence_ids: list[uuid.UUID]) -> None:
    have = {(e.source_evidence_id) for e in rs.evidence_at(session, req)}
    for ev in evidence_ids:
        if ev in have:
            continue
        session.add(RequirementEvidence(
            project_id=req.project_id, requirement_id=req.id, source_evidence_id=ev,
            relation=EvidenceRelation.confirms, introduced_in_revision=req.content_revision))
    session.flush()


def merge_candidates(session: Session, llm: GuardedLlm, project_id: uuid.UUID,
                     candidates: list[RequirementCandidate], run_id: uuid.UUID | None,
                     report: ExtractionReport) -> None:
    """Validate, dedupe and persist pending candidates in order."""
    adjudicate = load_prompt(ADJUDICATE_PROMPT)
    for cand in candidates:
        if cand.disposition != CandidateDisposition.pending:
            continue
        draft = _draft_to_dict(cand.payload)
        evidence_ids = [uuid.UUID(str(e)) for e in cand.payload["evidence_ids"]]
        existing = [r for r in rs.list_requirements(session, project_id, include_deprecated=False)]

        # 1) exact duplicate (normalized hash) -> attach confirming evidence only
        h = statement_hash(draft["statement"])
        exact = next((r for r in existing if r.normalized_hash == h), None)
        if exact:
            _link_confirming_evidence(session, exact, evidence_ids)
            cand.disposition = CandidateDisposition.merged_existing
            cand.merged_requirement_id = exact.id
            report.merged.append(exact.code)
            continue

        # 2) similar statements -> LLM adjudication of the (bounded) candidate pairs
        pairs = candidate_pairs(draft["statement"], [(r.code, r.statement) for r in existing])
        judgements = {}
        if pairs:
            body = [{"ref": "NEW", "text": draft["statement"]}] + [
                {"ref": code, "text": next(r.statement for r in existing if r.code == code)}
                for code, _ in pairs]
            result, flags = llm.run(
                purpose="adjudicate_pairs", prompt_id=adjudicate.prompt_id,
                prompt_version=adjudicate.version, system=adjudicate.text,
                instruction=("Judge NEW against each existing requirement. candidate_ref is NEW; "
                             "existing_code is one of: " + ", ".join(c for c, _ in pairs)),
                sources=body, schema=AdjudicationOutput)
            report.injection_flags.update(flags)
            allowed = {c for c, _ in pairs}
            judgements = {j.existing_code: j for j in result.parsed.judgements
                          if j.existing_code in allowed}
        dup = next((code for code, j in judgements.items() if j.relation == "duplicate"), None)
        if dup:
            target = next(r for r in existing if r.code == dup)
            _link_confirming_evidence(session, target, evidence_ids)
            cand.disposition = CandidateDisposition.merged_existing
            cand.merged_requirement_id = target.id
            report.merged.append(target.code)
            continue

        # 3) new requirement; vague or low-confidence ones need clarification
        vague = bool(draft.get("ambiguity_note")) or (draft.get("confidence") or 1) < LOW_CONFIDENCE
        req = rs.create_requirement(
            session, project_id, draft, evidence_ids, cause_type=CauseType.input_source,
            cause_id=str(cand.input_source_id), run_id=run_id,
            status=ReqStatus.needs_clarification if vague else ReqStatus.proposed)
        cand.disposition = CandidateDisposition.merged_new
        cand.merged_requirement_id = req.id
        report.created.append(req.code)
        _raise_quality_issues(session, project_id, req, draft, vague)
        for code, j in judgements.items():
            if j.relation == "conflict":
                other = next(r for r in existing if r.code == code)
                issue_service.raise_issue(
                    session, project_id, IssueType.conflict,
                    f"Conflicting requirements {req.code} and {code}", blocking=True,
                    description=j.reason, why_it_matters="Both cannot be satisfied at once.",
                    key=f"{min(req.code, code)}|{max(req.code, code)}",
                    requirement_ids=[req.id, other.id])
                report.conflicts.append((req.code, code))


def _raise_quality_issues(session: Session, project_id: uuid.UUID, req: Requirement, draft: dict,
                          vague: bool) -> None:
    if vague:
        issue_service.raise_issue(
            session, project_id, IssueType.ambiguity, f"{req.code} is ambiguous or unmeasurable",
            description=draft.get("ambiguity_note") or "Low extraction confidence.",
            why_it_matters="It cannot be objectively tested as written.",
            key=f"amb:{req.code}", requirement_ids=[req.id])
    if req.req_type.value == "FR" and not draft.get("acceptance_criteria"):
        issue_service.raise_issue(
            session, project_id, IssueType.missing_acceptance_criteria,
            f"{req.code} has no acceptance criteria", key=f"ac:{req.code}",
            why_it_matters="Without criteria the requirement cannot be verified.",
            requirement_ids=[req.id])
    if req.req_type.value == "FR" and not (draft.get("details") or {}).get("actors"):
        issue_service.raise_issue(
            session, project_id, IssueType.missing_info, f"{req.code}: actor not stated",
            key=f"actor:{req.code}", requirement_ids=[req.id],
            why_it_matters="It is unclear who performs or benefits from this function.")
