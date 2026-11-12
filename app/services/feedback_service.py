"""Compatibility alias: the implementation lives in app.agents.revision_agent.py."""
import sys

from app.agents import revision_agent as _impl

sys.modules[__name__] = _impl
