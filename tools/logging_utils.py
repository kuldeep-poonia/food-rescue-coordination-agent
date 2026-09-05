"""Structured JSON logging utilities with correlation ID threading and PII redaction."""

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from redaction import sanitize_payload_for_logging

# Thread-safe and async-safe context variable for correlation ID propagation
CORRELATION_ID_CONTEXT: ContextVar[str] = ContextVar(
    "correlation_id", default="unassigned"
)


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured JSON with automatic PII sanitization."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the specified record as a JSON string.

        Args:
            record: Standard Python LogRecord instance.

        Returns:
            JSON-serialized log message string.
        """
        correlation_id = getattr(
            record, "correlation_id", CORRELATION_ID_CONTEXT.get()
        )
        tool_name = getattr(record, "tool_name", record.name)

        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "tool_name": tool_name,
            "correlation_id": correlation_id,
            "message": record.getMessage(),
        }

        # Include custom extra details if provided, scrubbing PII
        if hasattr(record, "details") and isinstance(record.details, dict):
            payload["details"] = sanitize_payload_for_logging(record.details)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload)


def get_structured_logger(name: str) -> logging.Logger:
    """Obtain or configure a logger with the JSON formatter.

    Args:
        name: Name of the logger, typically __name__.

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
