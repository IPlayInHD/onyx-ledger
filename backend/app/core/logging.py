"""Structured JSON logging with correlation ids (structlog)."""
from __future__ import annotations

import logging
from contextvars import ContextVar

import structlog

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def _add_correlation_id(_, __, event_dict):
    event_dict["correlation_id"] = correlation_id.get()
    return event_dict


def configure_logging(debug: bool = False) -> None:
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if debug else logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "onyx"):
    return structlog.get_logger(name)
