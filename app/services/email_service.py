"""Compatibility alias: the implementation lives in app.agents.ingestion_agent.py."""
import sys

from app.agents import ingestion_agent as _impl

sys.modules[__name__] = _impl
