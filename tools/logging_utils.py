"""Structured JSON logging utilities with correlation ID threading and PII redaction."""

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from redaction import sanitize_payload_for_logging, sanitize_text_for_logging

# Thread-safe and async-safe context variable for correlation ID propagation
CORRELATION_ID_CONTEXT: ContextVar[str] = ContextVar(
    "correlation_id", default="unassigned"
)


def sanitize_correlation_id(raw_cid: Any) -> str:
    """Sanitize and bound correlation IDs to prevent log pollution or injection.

    Restricts to safe identifier characters (alphanumeric, dash, underscore)
    and bounds maximum length to 64 characters.

    Args:
        raw_cid: Untrusted or incoming correlation ID string/object.

    Returns:
        Cleaned, bounded correlation ID string.
    """
    if not raw_cid:
        return "unassigned"
    cid_str = str(raw_cid).strip()
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", cid_str)[:64]
    return cleaned or "unassigned"


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured JSON with automatic PII sanitization."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the specified record as a JSON string.

        Args:
            record: Standard Python LogRecord instance.

        Returns:
            JSON-serialized log message string.
        """
        raw_cid = getattr(record, "correlation_id", CORRELATION_ID_CONTEXT.get())
        correlation_id = sanitize_correlation_id(raw_cid)
        tool_name = getattr(record, "tool_name", record.name)
        raw_message = record.getMessage()
        clean_message = sanitize_text_for_logging(raw_message)

        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "tool_name": tool_name,
            "correlation_id": correlation_id,
            "message": clean_message,
        }

        # Include custom extra details if provided, scrubbing PII
        if hasattr(record, "details") and isinstance(record.details, dict):
            payload["details"] = sanitize_payload_for_logging(record.details)

        if record.exc_info:
            raw_exc = self.formatException(record.exc_info)
            payload["exception"] = sanitize_text_for_logging(raw_exc)
        elif getattr(record, "exc_text", None):
            payload["exception"] = sanitize_text_for_logging(record.exc_text)

        if getattr(record, "stack_info", None):
            raw_stack = self.formatStack(record.stack_info)
            payload["stack_info"] = sanitize_text_for_logging(raw_stack)

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
