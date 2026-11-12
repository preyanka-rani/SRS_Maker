"""LLM client interface, Gemini implementation (no tools), and a scripted fake for tests."""
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.llm.throttle import CallThrottle

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


class LlmError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def __reduce__(self):  # keeps the error picklable (Celery result/exception serialisation)
        return (LlmError, (self.code, str(self), self.retryable))


class LlmConsentMissing(LlmError):
    def __init__(self, scope: str):
        super().__init__("consent_missing", f"No active {scope} consent for this project")


@dataclass(frozen=True)
class LlmResult(Generic[T]):
    parsed: T
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    output_sha256: str = ""


class LlmClient(Protocol):
    model_id: str

    def generate(self, *, system: str, instruction: str, sources_payload: str | None,
                 schema: type[T]) -> LlmResult[T]: ...


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, LlmError) and exc.retryable


class GeminiClient:
    """google-genai wrapper. tools / function calling / code execution / search grounding /
    URL context are NEVER configured: the model can only return schema-valid JSON."""

    def __init__(self, api_key: str, model_id: str, timeout_s: float = 120.0,
                 max_retries: int = 4, max_rpm: int = 30):
        from google import genai

        self.model_id = model_id
        self._client = genai.Client(api_key=api_key)
        self._timeout_ms = int(timeout_s * 1000)
        self._max_retries = max_retries
        self._throttle = CallThrottle(max_rpm)

    def generate(self, *, system: str, instruction: str, sources_payload: str | None,
                 schema: type[T]) -> LlmResult[T]:
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            reraise=True,
        )
        def attempt() -> LlmResult[T]:
            self._throttle.acquire()
            return self._call(system, instruction, sources_payload, schema)

        return attempt()

    def _call(self, system, instruction, sources_payload, schema) -> LlmResult:
        from google.genai import errors, types

        contents = [instruction] + ([sources_payload] if sources_payload else [])
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            temperature=0.0,
            http_options=types.HttpOptions(timeout=self._timeout_ms),
        )
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self.model_id, contents=contents, config=config
            )
        except errors.APIError as exc:
            code = getattr(exc, "code", 0) or 0
            detail = str(getattr(exc, "message", "") or exc)[:300]
            log.warning("Gemini request failed: HTTP %s %s", code, detail)
            raise LlmError(f"gemini_http_{code}", f"Gemini request failed (HTTP {code}): {detail}",
                           retryable=code in {408, 429, 500, 502, 503, 504}) from exc
        except Exception as exc:  # noqa: BLE001 - network/timeouts
            raise LlmError("gemini_connection", "Cannot reach Gemini", retryable=True) from exc
        text = response.text or ""
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LlmError("schema_invalid", "Model output did not match the schema") from exc
        usage = getattr(response, "usage_metadata", None)
        return LlmResult(
            parsed=parsed, model_id=self.model_id,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            latency_ms=int((time.monotonic() - started) * 1000),
            output_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )


class FakeLlmClient:
    """Scripted client: queue responses (model instances or exceptions) per schema name."""

    model_id = "fake-llm"

    def __init__(self):
        self._queues: dict[str, list] = {}
        self.calls: list[dict] = []

    def enqueue(self, schema: type[BaseModel], response: BaseModel | Exception) -> None:
        self._queues.setdefault(schema.__name__, []).append(response)

    def generate(self, *, system: str, instruction: str, sources_payload: str | None,
                 schema: type[T]) -> LlmResult[T]:
        self.calls.append({"system": system, "instruction": instruction,
                           "sources_payload": sources_payload, "schema": schema.__name__})
        queue = self._queues.get(schema.__name__, [])
        if not queue:
            raise LlmError("fake_unscripted", f"No scripted response for {schema.__name__}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResult(parsed=item, model_id=self.model_id, input_tokens=10, output_tokens=10,
                         output_sha256=hashlib.sha256(item.model_dump_json().encode()).hexdigest())
