"""Compatibility alias: the implementation lives in app.agents.document_agent.py."""
import sys

from app.agents import document_agent as _impl

sys.modules[__name__] = _impl
