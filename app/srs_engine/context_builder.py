"""Build the typed render context for one SRS version from the database snapshot.

Everything here is deterministic. The only generated prose (the narrative) is passed in
already validated; requirement text always comes verbatim from the database.
"""
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    Client,
    IssueStatus,
    Project,
    RequirementIssue,
    SourceEvidence,
)
from app.services import requirement_service as rs

STATUS_LABELS = {
    "confirmed": "CONFIRMED",
    "proposed": "PROPOSED",
    "modified": "MODIFIED — PENDING CONFIRMATION",
    "needs_clarification": "NEEDS CLARIFICATION",
}
PRIORITY_LABELS = {"must": "Must", "should": "Should", "could": "Could", "wont": "Won't"}
TABLE_TYPES = {"NFR": "nfrs", "SEC": "secs", "BUS_RULE": "rules", "BR": "rules", "INT": "ints"}
OTHER_TYPES = ("CON", "ASM", "DEP", "REPORT", "UI_SCREEN", "TECH", "SCOPE_EXCLUSION", "FUTURE_SCOPE")
EXCERPT_LIMIT = 240


@dataclass(frozen=True)
class Narrative:
    executive_summary: str = ""
    purpose: str = ""
    scope: str = ""
    business_context: str = ""


@dataclass(frozen=True)
class DocControl:
    version: str
    status: str
    date: str
    prepared_by: str
    reviewers: str
    approvers: str


@dataclass(frozen=True)
class SrsContext:
    data: dict = field(default_factory=dict)


def _excerpts(session: Session, snap: dict) -> list[str]:
    ids = [uuid.UUID(link.split(":")[0]) for link in snap["evidence_link_ids"]]
    if not ids:
        return []
    rows = session.execute(select(SourceEvidence.excerpt).where(
        SourceEvidence.id.in_(ids)).limit(2)).scalars().all()
    return [r[:EXCERPT_LIMIT] + ("…" if len(r) > EXCERPT_LIMIT else "") for r in rows]


def _row(session: Session, snap: dict) -> dict:
    d = snap["details"] or {}
    return {
        "code": snap["code"], "type": snap["req_type"], "category": snap["category"] or "",
        "title": snap["title"], "statement": snap["statement"], "rationale": snap["rationale"] or "",
        "priority": PRIORITY_LABELS.get(snap["priority"] or "", "—"),
        "status_label": STATUS_LABELS.get(snap["status"], snap["status"].upper()),
        "purpose": d.get("purpose") or "", "actors": d.get("actors") or "",
        "trigger": d.get("trigger") or "", "preconditions": d.get("preconditions") or "",
        "main_flow": [str(s) for s in (d.get("main_flow") or [])],
        "acceptance": [{"given": a["given"], "when": a["when"], "then": a["then"]}
                       for a in snap["acceptance_criteria"]],
        "source_note": " | ".join(_excerpts(session, snap)) or "Client input",
    }


def _issue_rows(session: Session, project_id: uuid.UUID) -> list[dict]:
    rows = session.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project_id,
        RequirementIssue.status.in_([IssueStatus.open, IssueStatus.clarification_requested]))
        .order_by(RequirementIssue.created_at)).scalars().all()
    return [{"kind": i.issue_type.value.replace("_", " ").title(), "title": i.title,
             "description": i.description or "", "blocking": "Yes" if i.blocking else "No"}
            for i in rows]


def build_context(session: Session, project: Project, *, label: str, status: str,
                  narrative: Narrative, prepared_by: str, revisions: list[dict],
                  approver_names: str = "", final_approval: dict | None = None) -> SrsContext:
    client = session.get(Client, project.client_id)
    snaps = [s for s in rs.snapshots(session, project.id) if s["status"] != "deprecated"]
    grouped: dict[str, list[dict]] = {"frs": [], "nfrs": [], "secs": [], "rules": [], "ints": [],
                                      "others": []}
    for snap in snaps:
        row = _row(session, snap)
        if snap["req_type"] == "FR":
            grouped["frs"].append(row)
        elif snap["req_type"] in TABLE_TYPES:
            grouped[TABLE_TYPES[snap["req_type"]]].append(row)
        elif snap["req_type"] in OTHER_TYPES:
            grouped["others"].append(row)
    issues = _issue_rows(session, project.id)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    data = {
        "project": {"title": project.title, "subtitle": project.subtitle or "", "code": project.code,
                    "client": client.organisation_name if client else "", "token": project.project_token},
        "doc": {"version": label, "status": status, "date": today, "prepared_by": prepared_by,
                "reviewers": approver_names or "—", "approvers": approver_names or "—"},
        "revisions": revisions,
        "narrative": {"executive_summary": narrative.executive_summary,
                      "purpose": narrative.purpose, "scope": narrative.scope,
                      "business_context": narrative.business_context},
        **grouped,
        "issues": issues,
        "counts": {"total": len(snaps), "fr": len(grouped["frs"]), "issues": len(issues)},
        "final_approval": final_approval,
    }
    return SrsContext(data)
