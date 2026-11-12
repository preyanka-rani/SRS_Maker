import uuid

import pytest

from app.database.models import IssueType, WorkflowRun
from app.services import issue_service
from tests.conftest import login, make_user

API = "/api/v1"
BODY = {"req_type": "FR", "title": "Invoice export", "priority": "must",
        "statement": "The system shall export invoices as PDF.",
        "details": {"purpose": "Let finance archive invoices"},
        "acceptance_criteria": [{"given": "an invoice", "when": "exported", "then": "a PDF is produced"}]}


@pytest.fixture
def ctx(api, db):
    _, email, pw = make_user(db, "admin")
    h = login(api, email, pw)
    c = api.post(f"{API}/clients", json={"organisation_name": "Req Co"}, headers=h).json()["data"]
    p = api.post(f"{API}/projects", headers=h,
                 json={"client_id": c["id"], "code": "REQ-1", "title": "Req"}).json()["data"]
    return api, h, p["id"], db


def test_staff_create_get_list_and_filters(ctx):
    api, h, pid, _ = ctx
    r = api.post(f"{API}/projects/{pid}/requirements", json=BODY, headers=h)
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["code"] == "FR-001" and d["status"] == "proposed" and d["lock_version"] == 0
    got = api.get(f"{API}/projects/{pid}/requirements/FR-001", headers=h).json()["data"]
    assert got["details"]["purpose"]
    assert len(api.get(f"{API}/projects/{pid}/requirements?q=invoice", headers=h).json()["data"]) == 1
    assert api.get(f"{API}/projects/{pid}/requirements?q=zzz", headers=h).json()["data"] == []
    assert api.get(f"{API}/projects/{pid}/requirements?status=confirmed", headers=h).json()["data"] == []
    assert api.get(f"{API}/projects/{pid}/requirements/FR-404", headers=h).status_code == 404


def test_patch_requires_if_match_and_bumps_revision_with_history(ctx):
    api, h, pid, _ = ctx
    api.post(f"{API}/projects/{pid}/requirements", json=BODY, headers=h)
    url = f"{API}/projects/{pid}/requirements/FR-001"
    ok = api.patch(url, headers={**h, "If-Match": "0"},
                   json={"reason": "client asked for CSV too",
                         "statement": "The system shall export invoices as PDF or CSV."})
    assert ok.status_code == 200 and ok.json()["data"]["content_revision"] == 2
    stale = api.patch(url, headers={**h, "If-Match": "0"},
                      json={"reason": "stale edit", "title": "Stale title"})
    assert stale.status_code == 409
    hist = api.get(f"{url}/history", headers=h).json()["data"]
    assert [x["change_type"] for x in hist] == ["created", "modified"]
    assert hist[0]["snapshot"]["statement"].endswith("PDF.")  # the past is preserved
    assert api.patch(url, headers={**h, "If-Match": "1"}, json={"reason": "no-op"}).status_code == 422


def test_deprecate_then_edit_and_double_deprecate_are_rejected(ctx):
    api, h, pid, _ = ctx
    api.post(f"{API}/projects/{pid}/requirements", json=BODY, headers=h)
    url = f"{API}/projects/{pid}/requirements/FR-001"
    assert api.post(f"{url}/deprecate", json={"reason": "out of scope"}, headers=h).status_code == 200
    assert api.post(f"{url}/deprecate", json={"reason": "again please"}, headers=h).status_code == 409
    r = api.patch(url, headers={**h, "If-Match": "1"}, json={"reason": "edit", "title": "New title"})
    assert r.status_code == 422


def test_viewer_cannot_write_and_outsider_gets_404(ctx, db):
    api, h, pid, _ = ctx
    _, email, pw = make_user(db, "viewer")
    v = login(api, email, pw)
    assert api.post(f"{API}/projects/{pid}/requirements", json=BODY, headers=v).status_code == 403
    assert api.get(f"{API}/projects/{pid}/requirements", headers=v).status_code == 404


def test_issues_list_filter_and_resolution_rules(ctx):
    api, h, pid, db = ctx
    issue_service.raise_issue(db, uuid.UUID(pid), IssueType.ambiguity, "FR-009 is vague", blocking=False)
    issue_service.raise_issue(db, uuid.UUID(pid), IssueType.conflict, "A vs B", blocking=True)
    db.commit()
    assert len(api.get(f"{API}/projects/{pid}/issues", headers=h).json()["data"]) == 2
    blocking = api.get(f"{API}/projects/{pid}/issues?blocking=true", headers=h).json()["data"]
    assert [i["title"] for i in blocking] == ["A vs B"]
    iid = blocking[0]["id"]
    assert api.patch(f"{API}/projects/{pid}/issues/{iid}", json={"status": "resolved"},
                     headers=h).status_code == 422
    ok = api.patch(f"{API}/projects/{pid}/issues/{iid}", headers=h,
                   json={"status": "resolved", "resolution_text": "Client confirmed 7 years"})
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "resolved"


def test_manual_review_decisions_standalone_vs_graph(ctx):
    api, h, pid, db = ctx
    standalone = issue_service.open_manual_review(db, uuid.UUID(pid), "UNPROCESSED_ATTACHMENT")
    run = WorkflowRun(project_id=uuid.UUID(pid), thread_id="t", trigger_type="email")
    db.add(run)
    db.flush()
    graph = issue_service.open_manual_review(db, uuid.UUID(pid), "QA_FAILED", workflow_run_id=run.id)
    db.commit()
    assert len(api.get(f"{API}/manual-reviews", headers=h).json()["data"]) == 2
    bad = api.post(f"{API}/manual-reviews/{standalone.id}/decision", headers=h,
                   json={"decision": "regenerate", "note": "x"})
    assert bad.status_code == 422  # workflow-only decision on a standalone review
    ok = api.post(f"{API}/manual-reviews/{standalone.id}/decision", headers=h,
                  json={"decision": "dismiss", "note": "handled by phone"})
    assert ok.status_code == 202 and ok.json()["data"]["status"] == "applied"
    g = api.post(f"{API}/manual-reviews/{graph.id}/decision", headers=h,
                 json={"decision": "regenerate", "note": "retry generation"})
    assert g.json()["data"]["status"] == "resolved"  # waits for the workflow to apply it
    assert api.post(f"{API}/manual-reviews/{graph.id}/decision", headers=h,
                    json={"decision": "dismiss", "note": "again"}).status_code == 409


def test_workflow_status_activity_and_admin_requeue(ctx, db):
    api, h, pid, _ = ctx
    s = api.get(f"{API}/projects/{pid}/workflow", headers=h).json()["data"]
    assert s["pending_inputs"] == 0 and s["open_manual_reviews"] == 0 and s["runs"] == []
    assert api.post(f"{API}/projects/{pid}/start", headers=h).status_code == 202
    types = [e["type"] for e in api.get(f"{API}/projects/{pid}/activity", headers=h).json()["data"]]
    assert "start_requested" in types
    _, email, pw = make_user(db, "analyst")
    a = login(api, email, pw)
    assert api.post(f"{API}/projects/{pid}/workflow/requeue", json={"reason": "stuck run"},
                    headers=a).status_code in (403, 404)
    assert api.post(f"{API}/projects/{pid}/workflow/requeue", json={"reason": "stuck run"},
                    headers=h).status_code == 202
