"""Routing functions of the graph and the oldest-first input selection."""
from app.workflows.graph import (
    _route_after_approval as route_after_approval,
    _route_after_consent as route_after_consent,
    _route_after_email as route_after_email,
    _route_after_event as route_after_event,
)
from app.workflows.queue import next_trigger

__all__ = ["next_trigger", "route_after_approval", "route_after_consent", "route_after_email",
           "route_after_event"]
