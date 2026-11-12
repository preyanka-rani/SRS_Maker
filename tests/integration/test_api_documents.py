import io

import pytest

from app.integrations.transcription.base import FakeTranscriptionProvider, TranscriptionResult
from app.integrations.transcription.factory import set_provider_override
from app.processors.audio.transcript import AsrSegment
from tests.conftest import login, make_user
from tests.helpers import docx_bytes, make_audio, minimal_pdf

API = "/api/v1"


@pytest.fixture
def ctx(api, db):
    _, email, pw = make_user(db, "admin")
    h = login(api, email, pw)
    c = api.post(f"{API}/clients", json={"organisation_name": "Doc Co"}, headers=h).json()["data"]
    p = api.post(f"{API}/projects", headers=h,
                 json={"client_id": c["id"], "code": "DOC-1", "title": "Docs"}).json()["data"]
    yield api, h, p["id"]
    set_provider_override(None)


def _upload(api, h, pid, name, data, mime="application/octet-stream"):
    return api.post(f"{API}/projects/{pid}/documents", headers=h,
                    files={"file": (name, io.BytesIO(data), mime)})


def test_docx_upload_is_extracted_via_eager_task(ctx, tmp_path):
    api, h, pid = ctx
    r = _upload(api, h, pid, "req.docx", docx_bytes(tmp_path, ["Users shall log in."]))
    assert r.status_code == 202, r.text
    doc = api.get(f"{API}/projects/{pid}/documents/{r.json()['data']['id']}", headers=h).json()["data"]
    assert doc["extraction_status"] == "succeeded" and doc["processing_class"] == "text_supported"
    ev = api.get(f"{API}/projects/{pid}/documents/{doc['id']}/evidence", headers=h).json()["data"]
    assert ev[0]["excerpt"] == "Users shall log in."


def test_pdf_upload_and_listing_filters(ctx):
    api, h, pid = ctx
    assert _upload(api, h, pid, "a.pdf", minimal_pdf(["Some requirement text"])).status_code == 202
    listing = api.get(f"{API}/projects/{pid}/documents?status=succeeded", headers=h).json()["data"]
    assert len(listing) == 1 and listing[0]["page_count"] == 1


def test_duplicate_upload_is_409_with_existing_id(ctx, tmp_path):
    api, h, pid = ctx
    data = docx_bytes(tmp_path, ["dup"])
    first = _upload(api, h, pid, "a.docx", data).json()["data"]
    second = _upload(api, h, pid, "b.docx", data)
    assert second.status_code == 409
    assert second.json()["error"]["details"]["existing_id"] == first["id"]


@pytest.mark.parametrize("name", ["movie.mp4", "sheet.xlsx", "photo.png", "app.exe", "z.zip"])
def test_unsupported_upload_types_are_415(ctx, name):
    api, h, pid = ctx
    assert _upload(api, h, pid, name, b"x" * 100).status_code == 415


def test_empty_upload_is_422(ctx):
    api, h, pid = ctx
    assert _upload(api, h, pid, "e.txt", b"").status_code == 422


def test_other_projects_documents_are_404(ctx, db, tmp_path):
    api, h, pid = ctx
    doc = _upload(api, h, pid, "a.docx", docx_bytes(tmp_path, ["x"])).json()["data"]
    c = api.post(f"{API}/clients", json={"organisation_name": "Other"}, headers=h).json()["data"]
    other = api.post(f"{API}/projects", headers=h,
                     json={"client_id": c["id"], "code": "OTH", "title": "O"}).json()["data"]
    assert api.get(f"{API}/projects/{other['id']}/documents/{doc['id']}", headers=h).status_code == 404


def test_audio_upload_blocked_without_consent_then_transcribed_after_grant(ctx, tmp_path):
    api, h, pid = ctx
    audio = make_audio(tmp_path / "v.wav", [("tone", 10)]).read_bytes()
    provider = FakeTranscriptionProvider([TranscriptionResult(
        (AsrSegment(0, 8, "Customers need invoices by email", -0.2, 0.01),), "en", 10.0)])
    set_provider_override(provider)

    doc = _upload(api, h, pid, "v.wav", audio, "audio/wav").json()["data"]
    state = api.get(f"{API}/projects/{pid}/documents/{doc['id']}", headers=h).json()["data"]
    assert state["extraction_status"] == "awaiting_consent" and provider.calls == []
    assert api.get(f"{API}/projects/{pid}/documents/{doc['id']}/transcript", headers=h).status_code == 409
