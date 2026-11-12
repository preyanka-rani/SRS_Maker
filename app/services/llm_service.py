"""Consent-gated, ledgered access to the LLM. The only way agents may call Gemini."""
import hashlib
import uuid
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import ConsentScope, LlmCall, ProjectLlmConsent
from app.llm.client import LlmClient, LlmConsentMissing, LlmError, LlmResult
from app.llm.output_schemas import SCHEMA_VERSION
from app.llm.untrusted_serializer import SYSTEM_GUARD, serialize_sources

T = TypeVar("T", bound=BaseModel)


def has_consent(session: Session, project_id: uuid.UUID, scope: ConsentScope) -> bool:
    return session.execute(
        select(ProjectLlmConsent.id).where(
            ProjectLlmConsent.project_id == project_id,
            ProjectLlmConsent.scope == scope,
            ProjectLlmConsent.revoked_at.is_(None),
        )
    ).first() is not None


class GuardedLlm:
    def __init__(self, session: Session, client: LlmClient, project_id: uuid.UUID,
                 workflow_run_id: uuid.UUID | None = None):
        self._session = session
        self._client = client
        self._project_id = project_id
        self._run_id = workflow_run_id

    def run(self, *, purpose: str, prompt_id: str, prompt_version: str, system: str,
            instruction: str, sources: list[dict] | None, schema: type[T]
            ) -> tuple[LlmResult[T], tuple[str, ...]]:
        """Returns (result, injection_flags). Raises LlmConsentMissing without consent."""
        if not has_consent(self._session, self._project_id, ConsentScope.text_processing):
            raise LlmConsentMissing(ConsentScope.text_processing.value)
        serialized = serialize_sources(sources) if sources else None
        flags = serialized.flags if serialized else ()
        input_hash = hashlib.sha256(
            (instruction + (serialized.payload if serialized else "")).encode()
        ).hexdigest()
        call = LlmCall(
            project_id=self._project_id, workflow_run_id=self._run_id, provider="gemini",
            purpose=purpose, model_id=self._client.model_id, prompt_id=prompt_id,
            prompt_version=prompt_version, output_schema_version=f"{schema.__name__}@{SCHEMA_VERSION}",
            input_sha256=input_hash, status="error", injection_flags=list(flags) or None,
        )
        self._session.add(call)
        self._session.flush()
        try:
            result = self._client.generate(
                system=f"{system}\n\n{SYSTEM_GUARD}", instruction=instruction,
                sources_payload=serialized.payload if serialized else None, schema=schema,
            )
        except LlmError as exc:
            call.status = "schema_invalid" if exc.code == "schema_invalid" else "error"
            self._session.flush()
            raise
        call.status = "ok"
        call.input_tokens = result.input_tokens
        call.output_tokens = result.output_tokens
        call.latency_ms = result.latency_ms
        call.output_sha256 = result.output_sha256
        self._session.flush()
        return result, flags
