"""Compatibility alias: the implementation lives in app.agents.email_agent.py."""
import sys

from app.agents import email_agent as _impl

sys.modules[__name__] = _impl
