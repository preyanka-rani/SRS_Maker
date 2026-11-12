"""Compatibility re-exports: extraction lives in agents.extraction_agent; validation, dedupe and
persistence live in agents.validation_agent."""
from app.agents.extraction_agent import EXTRACT_PROMPT, ExtractionReport, extract_candidates  # noqa: F401
from app.agents.validation_agent import ADJUDICATE_PROMPT, LOW_CONFIDENCE, merge_candidates  # noqa: F401
from app.database.models import IssueType  # noqa: F401
