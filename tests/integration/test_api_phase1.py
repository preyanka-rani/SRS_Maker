import uuid

from tests.conftest import login, make_user

API = "/api/v1"


def _setup(api, db, role="admin"):
    _, email, pw = make_user(db, role)
    return login(api, email, pw)


def _client(api, h, name="Acme Ltd"):
    r = api.post(f"{API}/clients", json={"organisation_name": name}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _project(api, h, client_id, code="NW-1"):
    r = api.post(f"{API}/projects", headers=h,
                 json={"client_id": client_id, "code": code, "title": "Northwind"})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_login_flow_and_bad_credentials(api, db):
    _, email, pw = make_user(db, "admin")
    assert api.post(f"{API}/auth/token", json={"email": email, "password": "nope"}).status_code == 401
    assert api.post(f"{API}/auth/token",
                    json={"email": "ghost@example.com", "password": pw}).status_code == 401
    r = api.post(f"{API}/auth/token", json={"email": email.upper(), "password": pw})
    assert r.status_code == 200 and r.json()["data"]["role"] == "admin"


def test_unauthenticated_and_viewer_cannot_write(api, db):
    assert api.post(f"{API}/clients", json={"organisation_name": "X"}).status_code == 401
    h = _setup(api, db, "viewer")
    assert api.post(f"{API}/clients", json={"organisation_name": "X"}, headers=h).status_code == 403


def test_duplicate_client_name_is_conflict_case_insensitive(api, db):
    h = _setup(api, db)
    _client(api, h, "Acme")
    r = api.post(f"{API}/clients", json={"organisation_name": "ACME"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_contacts_normalized_and_unique(api, db):
    h = _setup(api, db)
    c = _client(api, h)
    r = api.post(f"{API}/clients/{c['id']}/contacts", headers=h,
                 json={"full_name": "Ann", "email": "Ann@Example.COM"})
    assert r.status_code == 201 and r.json()["data"]["email"] == "ann@example.com"
    dup = api.post(f"{API}/clients/{c['id']}/contacts", headers=h,
                   json={"full_name": "Ann 2", "email": "ann@example.com"})
    assert dup.status_code == 409
    got = api.get(f"{API}/clients/{c['id']}", headers=h).json()["data"]
    assert len(got["contacts"]) == 1


def test_project_token_format_and_code_uniqueness(api, db):
    h = _setup(api, db)
    c = _client(api, h)
    p = _project(api, h, c["id"])
    assert p["project_token"].startswith("PRJ-") and len(p["project_token"]) == 10
    r = api.post(f"{API}/projects", headers=h,
                 json={"client_id": c["id"], "code": "NW-1", "title": "Again"})
    assert r.status_code == 409


def test_non_members_get_404_and_see_no_projects(api, db):
    admin = _setup(api, db, "admin")
    c = _client(api, admin)
    p = _project(api, admin, c["id"])
    analyst = _setup(api, db, "analyst")
    assert api.get(f"{API}/projects/{p['id']}", headers=analyst).status_code == 404
    assert api.get(f"{API}/projects", headers=analyst).json()["data"] == []
    assert len(api.get(f"{API}/projects", headers=admin).json()["data"]) == 1


def test_optimistic_locking_on_project_update(api, db):
    h = _setup(api, db)
    p = _project(api, h, _client(api, h)["id"])
    ok = api.patch(f"{API}/projects/{p['id']}", headers={**h, "If-Match": "0"},
                   json={"title": "New"})
    assert ok.status_code == 200 and ok.json()["data"]["lock_version"] == 1
    stale = api.patch(f"{API}/projects/{p['id']}", headers={**h, "If-Match": "0"},
                      json={"title": "Stale"})
    assert stale.status_code == 409


def test_contact_of_other_client_cannot_be_authorized(api, db):
    h = _setup(api, db)
    a, b = _client(api, h, "A Co"), _client(api, h, "B Co")
    p = _project(api, h, a["id"])
    bob = api.post(f"{API}/clients/{b['id']}/contacts", headers=h,
                   json={"full_name": "Bob", "email": "bob@b.com"}).json()["data"]
    r = api.post(f"{API}/projects/{p['id']}/contacts", headers=h,
                 json={"contact_id": bob["id"], "role": "approver", "can_approve": True})
    assert r.status_code == 422


def test_can_approve_requires_approver_role_and_revoke_works(api, db):
    h = _setup(api, db)
    c = _client(api, h)
    p = _project(api, h, c["id"])
    ann = api.post(f"{API}/clients/{c['id']}/contacts", headers=h,
                   json={"full_name": "Ann", "email": "ann@a.com"}).json()["data"]
    bad = api.post(f"{API}/projects/{p['id']}/contacts", headers=h,
                   json={"contact_id": ann["id"], "role": "reviewer", "can_approve": True})
    assert bad.status_code == 422
    good = api.post(f"{API}/projects/{p['id']}/contacts", headers=h,
                    json={"contact_id": ann["id"], "role": "approver", "can_approve": True})
    assert good.status_code == 201
    assert api.delete(f"{API}/projects/{p['id']}/contacts/{ann['id']}", headers=h).status_code == 204
    assert api.delete(f"{API}/projects/{p['id']}/contacts/{ann['id']}", headers=h).status_code == 404


def test_consent_grant_conflict_revoke_and_regrant(api, db):
    h = _setup(api, db)
    p = _project(api, h, _client(api, h)["id"])
    body = {"scope": "text_processing", "policy_reference": "DPA-2026-01"}
    assert api.post(f"{API}/projects/{p['id']}/consents", json=body, headers=h).status_code == 201
    assert api.post(f"{API}/projects/{p['id']}/consents", json=body, headers=h).status_code == 409
    rev = api.request("DELETE", f"{API}/projects/{p['id']}/consents/text_processing",
                      json={"reason": "client request"}, headers=h)
    assert rev.status_code == 200 and rev.json()["data"]["revoked_at"]
    assert api.post(f"{API}/projects/{p['id']}/consents", json=body, headers=h).status_code == 201
    history = api.get(f"{API}/projects/{p['id']}/consents", headers=h).json()["data"]
    assert len(history) == 2


def test_analyst_cannot_grant_consent(api, db):
    admin = _setup(api, db, "admin")
    p = _project(api, admin, _client(api, admin)["id"])
    analyst = _setup(api, db, "analyst")
    r = api.post(f"{API}/projects/{p['id']}/consents", headers=analyst,
                 json={"scope": "audio_processing", "policy_reference": "x"})
    assert r.status_code in (403, 404)


def test_validation_errors_use_envelope(api, db):
    h = _setup(api, db)
    r = api.post(f"{API}/projects", headers=h, json={"code": "x"})
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False and body["error"]["code"] == "validation_failed"


def test_health_live_and_core_ready_semantics(api, db):
    assert api.get(f"{API}/health/live").json()["success"] is True
    r = api.get(f"{API}/health/ready")  # no Redis in this test environment
    assert r.status_code == 503
    assert r.json()["error"]["details"]["database"] == "ok"
    h = _setup(api, db)
    deps = api.get(f"{API}/health/dependencies", headers=h)
    assert deps.status_code == 200 and "gemini" in deps.json()["data"]


def test_unknown_project_is_404(api, db):
    h = _setup(api, db)
    assert api.get(f"{API}/projects/{uuid.uuid4()}", headers=h).status_code == 404
