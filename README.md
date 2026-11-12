# Agentic SRS Generation System

Email the system mailbox a project request (text, PDF, DOCX or audio). The system opens a
project, extracts requirements, generates a Software Requirements Specification (Word + PDF),
emails it back, applies the client's change requests as new versions, and — after an explicit,
verified approval — sends the final v1.0.

## Run locally (no Docker, no Redis)

```powershell
.venv\Scripts\python -m pip install -r requirements.lock.txt   # once
copy .env.example .env                                        # then fill in real values
.venv\Scripts\python main.py
```

`.env` needs: `IMAP_*` / `SMTP_*` (the system mailbox; for Gmail use an app password),
`GEMINI_API_KEY`, `GEMINI_MODEL_REASONING`, optionally `GROQ_API_KEY` (audio) and
`ADMIN_EMAIL` / `ADMIN_PASSWORD` (first staff login). Secrets stay in `.env` (git-ignored).

`main.py` starts an embedded PostgreSQL in `.pgdata/`, migrates, and serves
<http://127.0.0.1:8000/docs>. The in-process scheduler then polls the mailbox every
`IMAP_POLL_INTERVAL_SECONDS`, runs the workflow, and sends replies every 15 s.

## What happens to an email

| Email | Result |
|---|---|
| From someone new, no project token | New project opened, acknowledgement sent, SRS draft v0.1 emailed |
| Reply to an SRS email asking for changes | Requirements updated (history kept), SRS v0.2 emailed |
| Reply with a question | Drafted answer goes to staff review (`REPLY_REVIEW`) |
| Reply approving the current version | 8 checks → final v1.0 built from the approved snapshot and emailed |
| Token/headers present but sender not authorised | Not bound; manual review `THREAD_BINDING_FAILED` |

Subjects follow `[PRJ-XXXXXX] <title> – SRS v<label> …`; clients should keep the subject on reply.

## Layout

`app/api/v1` (HTTP) · `app/agents` (LLM-facing steps) · `app/workflows` (LangGraph) ·
`app/services` (domain logic) · `app/srs_engine` (template → DOCX/PDF, QA, versions) ·
`app/processors` (PDF/DOCX/audio) · `app/integrations` (email, storage, transcription) ·
`templates/` (Clean Base Template, manifest, leakage denylist) · `tests/`.

Rebuild the template with `python -m app.srs_engine.template_builder`.

## Tests

`.venv\Scripts\python -m pytest tests -q` (embedded PostgreSQL; fake LLM/IMAP/SMTP).

## Notes

- PDF export uses Gotenberg when `GOTENBERG_URL` is set, otherwise a built-in simpler PDF.
- Docker files are kept for later deployment but are not needed locally.
- Licences: no AGPL/GPL dependencies (PyMuPDF is not used); `docxtpl` is LGPL, used unmodified.
