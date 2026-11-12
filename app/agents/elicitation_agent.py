"""elicitation_agent: finds what a thin SRS is still missing and asks the client for it.

A deterministic checklist decides WHICH categories are missing (so the result never depends on
model mood). The model only words one specific question per missing category, grounded in the
requirements already captured; if it is unavailable, standard questions are used instead.
Open questions are stored as non-blocking `missing_info` issues: they appear in the SRS gap
section and in the review email, and are resolved automatically once the client's answers
produce matching requirements.
"""
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import IssueStatus, IssueType, Project, RequirementIssue
from app.llm.client import LlmError
from app.llm.output_schemas import ClarifyingQuestionsOutput
from app.llm.prompt_registry import Prompt, load_prompt
from app.services import issue_service
from app.services.llm_service import GuardedLlm

PROMPT_ID = "elicit_questions"
MAX_TITLE = 500


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    default_question: str
    why: str
    satisfied: Callable[[list[dict]], bool]


def _has(*types: str) -> Callable[[list[dict]], bool]:
    return lambda snaps: any(s["req_type"] in types for s in snaps)


def _has_actor(snaps: list[dict]) -> bool:
    return any(s["req_type"] == "FR" and (s["details"] or {}).get("actors") for s in snaps)


TOPICS: tuple[Topic, ...] = (
    Topic("roles", "User roles",
          "Who will use the system, and what is each type of user (role) allowed to do?",
          "Roles decide permissions, screens and workflows.", _has_actor),
    Topic("nfr", "Performance, availability and usability",
          "What are your expectations for speed, number of users, availability (uptime) and ease of use?",
          "Non-functional targets shape the architecture and the acceptance tests.", _has("NFR")),
    Topic("security", "Security and privacy",
          "How should users sign in, and is there any sensitive data or compliance rule we must respect?",
          "Security needs are expensive to add late.", _has("SEC")),
    Topic("integrations", "Integrations",
          "Does the system need to connect to other systems, such as payment, email/SMS, ERP or "
          "government services?",
          "Integrations drive effort and risk.", _has("INT")),
    Topic("business_rules", "Business rules",
          "Which business rules or policies must the system enforce, such as approvals, limits, "
          "calculations or deadlines?",
          "Rules are where systems most often differ from expectations.", _has("BUS_RULE", "BR")),
    Topic("constraints", "Constraints, assumptions and dependencies",
          "Are there any constraints (budget, deadline, technology, hosting), assumptions or "
          "dependencies on other teams or vendors?",
          "They bound the solution and the plan.", _has("CON", "ASM", "DEP")),
    Topic("scope", "Scope boundaries",
          "Is there anything that is explicitly out of scope now, or planned for a later phase?",
          "A clear boundary prevents scope creep.", _has("SCOPE_EXCLUSION", "FUTURE_SCOPE")),
)
BY_KEY = {t.key: t for t in TOPICS}


def prompt() -> Prompt:
    return load_prompt(PROMPT_ID)


def missing_topics(snaps: list[dict]) -> list[Topic]:
    return [t for t in TOPICS if not t.satisfied(snaps)]


def _issue(session: Session, project_id: uuid.UUID, key: str) -> RequirementIssue | None:
    fp = issue_service.fingerprint(project_id, IssueType.missing_info.value, f"elicit:{key}")
    return session.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project_id,
        RequirementIssue.fingerprint == fp)).scalar_one_or_none()


def _word_questions(llm: GuardedLlm, project: Project, snaps: list[dict],
                    topics: list[Topic]) -> dict[str, str]:
    """One question per topic: model wording when possible, standard wording otherwise."""
    questions = {t.key: t.default_question for t in topics}
    p = prompt()
    payload = {"missing_categories": [t.key for t in topics],
               "requirements": [{"code": s["code"], "type": s["req_type"], "title": s["title"],
                                 "statement": s["statement"]} for s in snaps]}
    try:
        result, _ = llm.run(
            purpose="elicit_questions", prompt_id=p.prompt_id, prompt_version=p.version,
            system=p.text, instruction=f"Project title: {project.title}",
            sources=[{"ref": "R1", "text": str(payload), "lang": "en", "origin": "requirements"}],
            schema=ClarifyingQuestionsOutput)
    except LlmError:
        return questions
    for q in result.parsed.questions:
        if q.category in questions:
            questions[q.category] = q.question.strip()
    return questions


def update_questions(session: Session, llm: GuardedLlm, project: Project, snaps: list[dict]) -> int:
    """Resolve questions the requirements now answer; open one question per missing topic.

    Idempotent: a topic that already has a question never triggers another model call.
    Returns the number of questions currently open.
    """
    for topic in TOPICS:
        issue = _issue(session, project.id, topic.key)
        if issue is not None and topic.satisfied(snaps) and issue.status in (
                IssueStatus.open, IssueStatus.clarification_requested):
            issue.status = IssueStatus.resolved
            issue.resolution_text = "Covered by the requirements captured from the client's answers."
    fresh = [t for t in missing_topics(snaps) if _issue(session, project.id, t.key) is None]
    if fresh:
        questions = _word_questions(llm, project, snaps, fresh)
        for topic in fresh:
            issue_service.raise_issue(
                session, project.id, IssueType.missing_info, questions[topic.key][:MAX_TITLE],
                blocking=False, description=f"Missing information: {topic.label}.",
                why_it_matters=topic.why, key=f"elicit:{topic.key}")
    session.flush()
    return len(session.execute(select(RequirementIssue.id).where(
        RequirementIssue.project_id == project.id, RequirementIssue.issue_type == IssueType.missing_info,
        RequirementIssue.status == IssueStatus.open)).all())
