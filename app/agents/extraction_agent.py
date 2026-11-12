"""extraction_agent: Gemini requirement extraction. Evidence refs are checked against the
allowlist sent in the request; output is a PROPOSAL (candidates), never a confirmed requirement."""
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database.models import (
    CandidateDisposition,
    InputSource,
    RequirementCandidate,
    SourceEvidence,
)
from app.llm.output_schemas import ExtractionOutput
from app.llm.prompt_registry import load_prompt
from app.processors.chunking import build_chunks
from app.services.llm_service import GuardedLlm

EXTRACT_PROMPT = "extract_requirements"


@dataclass
class ExtractionReport:
    candidates: int = 0
    created: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    rejected: int = 0
    injection_flags: set[str] = field(default_factory=set)


def _evidence_for(session: Session, source_ids: list[uuid.UUID]) -> list[SourceEvidence]:
    return list(session.execute(
        select(SourceEvidence).where(SourceEvidence.input_source_id.in_(source_ids))
        .order_by(SourceEvidence.input_source_id, SourceEvidence.seq)).scalars())


def extract_candidates(session: Session, llm: GuardedLlm, project_id: uuid.UUID,
                       source_ids: list[uuid.UUID], run_id: uuid.UUID | None,
                       report: ExtractionReport) -> list[RequirementCandidate]:
    """One structured call per chunk. Idempotent per (project, chunk_key)."""
    prompt = load_prompt(EXTRACT_PROMPT)
    out: list[RequirementCandidate] = []
    for source_id in source_ids:
        source = session.get(InputSource, source_id)
        rows = _evidence_for(session, [source_id])
        items = [(str(r.id), r.excerpt) for r in rows]
        lang = {str(r.id): r.language for r in rows}
        for chunk in build_chunks(items):
            key = f"{source_id}:{chunk.index}:{prompt.version}"
            existing = session.execute(select(RequirementCandidate).where(
                RequirementCandidate.project_id == project_id,
                or_(RequirementCandidate.chunk_key == key,
                    RequirementCandidate.chunk_key.like(key + ":%")))).scalars().all()
            if existing:
                out.extend(existing)
                continue
            refs = {f"E{i}": ev_id for i, ev_id in enumerate(chunk.evidence_ids, start=1)}
            sources = [{"ref": ref, "text": text, "lang": lang.get(ev_id),
                        "origin": source.original_filename or "message"}
                       for (ref, ev_id), text in zip(refs.items(), chunk.texts)]
            result, flags = llm.run(
                purpose="extract_requirements", prompt_id=prompt.prompt_id,
                prompt_version=prompt.version, system=prompt.text,
                instruction=("Extract the requirements from the sources. Valid evidence refs: "
                             + ", ".join(refs)),
                sources=sources, schema=ExtractionOutput)
            report.injection_flags.update(flags)
            for draft in result.parsed.requirements:
                report.candidates += 1
                valid = [refs[r] for r in draft.evidence_refs if r in refs]
                payload = draft.model_dump()
                payload["evidence_ids"] = valid
                disposition = (CandidateDisposition.pending if valid
                               else CandidateDisposition.rejected_invalid)
                if not valid:
                    report.rejected += 1
                row = RequirementCandidate(
                    project_id=project_id, workflow_run_id=run_id, input_source_id=source_id,
                    chunk_key=f"{key}:{len(out)}", payload=payload, disposition=disposition)
                session.add(row)
                out.append(row)
            if not result.parsed.requirements:  # remember the empty chunk so it is not re-asked
                session.add(RequirementCandidate(
                    project_id=project_id, workflow_run_id=run_id, input_source_id=source_id,
                    chunk_key=key, payload={"empty": True},
                    disposition=CandidateDisposition.rejected_invalid))
            session.flush()
    return out
