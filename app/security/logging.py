"""Structured JSON logging with automatic PII redaction (Constitution Section 3.4)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

# Regex patterns for redacting emails and phone numbers in logs
_EMAIL_PATTERN = re.compile(r"\b([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*?)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}\b"
)


def redact_pii(text: str) -> str:
    """Mask email addresses and phone numbers in free-text strings for safe logging."""
    if not text:
        return text

    def _mask_email(match: re.Match[str]) -> str:
        first_char = match.group(1)
        domain = match.group(3)
        return f"{first_char}***@{domain}"

    def _mask_phone(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7 or len(digits) > 15:
            return raw
        return f"***-***-{digits[-4:]}"

    redacted = _EMAIL_PATTERN.sub(_mask_email, text)
    redacted = _PHONE_PATTERN.sub(_mask_phone, redacted)
    return redacted


class PIIRedactingJSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON with automatic PII scrubbing."""

    def format(self, record: logging.LogRecord) -> str:
        message = redact_pii(record.getMessage())
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if hasattr(record, "enquiry_id"):
            payload["enquiry_id"] = getattr(record, "enquiry_id")
        if hasattr(record, "correlation_id"):
            payload["correlation_id"] = getattr(record, "correlation_id")
        if record.exc_info:
            payload["exception"] = redact_pii(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """Create or retrieve a configured logger with PII redaction."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PIIRedactingJSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger

