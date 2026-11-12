"""Structured JSON logging with secret redaction."""
import logging

import structlog

_SENSITIVE = {"password", "secret", "token", "api_key", "authorization"}


def _redact(_, __, event_dict: dict) -> dict:
    for key in list(event_dict):
        if any(s in key.lower() for s in _SENSITIVE):
            event_dict[key] = "***"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
