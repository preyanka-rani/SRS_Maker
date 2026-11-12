from sqlalchemy import select

from app.agents import elicitation_agent as ea
from app.database.models import (
    CauseType,
    ConsentScope,
    IssueStatus,
    IssueType,
    ProjectLlmConsent,
    RequirementIssue,
)
from app.llm.client import FakeLlmClient
from app.llm.output_schemas import ClarifyingQuestion, ClarifyingQuestionsOutput
from app.services import requirement_service as rs
from app.services.llm_service import GuardedLlm
from tests.helpers import make_project


def _fr(db, project, statement="The system shall export invoices as PDF.", actors=None):
    return rs.create_requirement(
        db, project.id, {"req_type": "FR", "title": "Invoice export", "statement": statement,
                         "details": {"actors": actors}}, [], cause_type=CauseType.staff_edit)


def _open(db, project):
    return {i.fingerprint: i for i in db.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project.id,
        RequirementIssue.issue_type == IssueType.missing_info)).scalars()}


def test_thin_input_gets_one_question_per_missing_topic_and_is_idempotent(db):
    project = make_project(db)
    db.add(ProjectLlmConsent(project_id=project.id, scope=ConsentScope.text_processing,
                             policy_reference="DPA"))
    _fr(db, project)
    fake = FakeLlmClient()
    fake.enqueue(ClarifyingQuestionsOutput, ClarifyingQuestionsOutput(questions=[
        ClarifyingQuestion(category="nfr", question="How many finance users will export invoices at once?"),
        ClarifyingQuestion(category="not_a_topic", question="This category must be ignored by code.")]))
    llm = GuardedLlm(db, fake, project.id)
    snaps = rs.snapshots(db, project.id)

    assert ea.update_questions(db, llm, project, snaps) == len(ea.TOPICS)  # all seven are missing
    titles = [i.title for i in _open(db, project).values()]
    assert "How many finance users will export invoices at once?" in titles  # model wording used
    assert ea.BY_KEY["security"].default_question in titles  # standard wording as fallback
    calls = len(fake.calls)
    assert ea.update_questions(db, llm, project, snaps) == len(ea.TOPICS)
    assert len(fake.calls) == calls  # nothing new to ask: no model call


def test_questions_are_resolved_when_requirements_cover_the_topic(db):
    project = make_project(db)
    db.add(ProjectLlmConsent(project_id=project.id, scope=ConsentScope.text_processing,
                             policy_reference="DPA"))
    _fr(db, project)
    llm = GuardedLlm(db, FakeLlmClient(), project.id)  # unscripted -> standard questions
    ea.update_questions(db, llm, project, rs.snapshots(db, project.id))
    rs.create_requirement(db, project.id, {"req_type": "NFR", "category": "PERF", "title": "Speed",
                                           "statement": "Pages shall load within 2 seconds."}, [],
                          cause_type=CauseType.feedback)
    rs.create_requirement(db, project.id, {"req_type": "SEC", "title": "Login",
                                           "statement": "Users shall sign in with two-factor authentication."},
                          [], cause_type=CauseType.feedback)
    remaining = ea.update_questions(db, llm, project, rs.snapshots(db, project.id))
    assert remaining == len(ea.TOPICS) - 2
    by_status = {i.status for i in _open(db, project).values()}
    assert IssueStatus.resolved in by_status and IssueStatus.open in by_status
    resolved = [i for i in _open(db, project).values() if i.status == IssueStatus.resolved]
    assert len(resolved) == 2 and all(i.resolution_text for i in resolved)


def test_roles_question_is_satisfied_by_an_actor(db):
    project = make_project(db)
    _fr(db, project, actors="Finance user")
    assert "roles" not in [t.key for t in ea.missing_topics(rs.snapshots(db, project.id))]
