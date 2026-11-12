"""srs_writer: narrative prose only. Requirement text is never generated here."""
from app.database.models import Project
from app.llm.client import LlmConsentMissing, LlmError
from app.llm.output_schemas import SrsNarrativeOutput
from app.llm.prompt_registry import Prompt, load_prompt
from app.services.llm_service import GuardedLlm
from app.srs_engine.context_builder import Narrative

PROMPT_ID = "write_srs_narrative"


def prompt() -> Prompt:
    return load_prompt(PROMPT_ID)


def fallback_narrative(project: Project, snaps: list[dict]) -> Narrative:
    """Deterministic text used when the model is unavailable. States only counted facts."""
    kinds: dict[str, int] = {}
    for s in snaps:
        kinds[s["req_type"]] = kinds.get(s["req_type"], 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "no requirements"
    return Narrative(
        executive_summary=f"This document specifies the requirements captured so far for "
                          f"{project.title}: {summary}.",
        purpose=f"This Software Requirements Specification records the requirements of "
                f"{project.title} as understood from the client's input.",
        scope="The scope is defined by the requirements listed in this document. Items marked "
              "as proposed or needing clarification are still to be confirmed with the client.",
        business_context="Business context will be refined as the client provides more detail.")


def write_narrative(llm: GuardedLlm, project: Project, snaps: list[dict]) -> tuple[Narrative, str]:
    """Returns (narrative, method) where method is 'L' (model) or 'D' (deterministic fallback)."""
    p = prompt()
    payload = [{"code": s["code"], "type": s["req_type"], "title": s["title"],
                "statement": s["statement"]} for s in snaps]
    try:
        result, _ = llm.run(
            purpose="write_srs_narrative", prompt_id=p.prompt_id, prompt_version=p.version,
            system=p.text, instruction=f"Project title: {project.title}",
            sources=[{"ref": "R1", "text": str(payload), "lang": "en", "origin": "requirements"}],
            schema=SrsNarrativeOutput)
    except LlmConsentMissing:
        raise
    except LlmError:
        return fallback_narrative(project, snaps), "D"
    out = result.parsed
    return Narrative(out.executive_summary, out.purpose, out.scope, out.business_context), "L"
