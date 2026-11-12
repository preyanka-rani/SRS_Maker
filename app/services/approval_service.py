"""Compatibility alias: the implementation lives in app.agents.approval_agent.py."""
import sys

from app.agents import approval_agent as _impl

sys.modules[__name__] = _impl
