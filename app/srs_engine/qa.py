"""Compatibility alias: the implementation lives in app.agents.qa_agent.py."""
import sys

from app.agents import qa_agent as _impl

sys.modules[__name__] = _impl
