"""Dependencies injected into workflow nodes (keeps nodes testable and provider-agnostic)."""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.storage.local import LocalStorage
from app.llm.client import LlmClient


@dataclass(frozen=True)
class WorkflowDeps:
    settings: Settings
    storage: LocalStorage
    session_scope: Callable[[], AbstractContextManager[Session]]
    llm_client: Callable[[], LlmClient]
