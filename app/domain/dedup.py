"""Idempotency normalization and SHA-256 deduplication fingerprinting (Constitution Section 2.3)."""

from __future__ import annotations

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")
_NON_DIGIT_RE = re.compile(r"[^\d+]")


def normalize_email(email: str | None) -> str:
    """Normalize email address to lowercase trimmed string."""
    if not email:
        return ""
    return email.strip().lower()


def normalize_phone(phone: str | None) -> str:
    """Normalize phone number by stripping spaces, dashes, and parentheses."""
    if not phone:
        return ""
    cleaned = _NON_DIGIT_RE.sub("", phone.strip())
    # Strip leading 00 in international prefix and standardize
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


def normalize_body(body: str) -> str:
    """Collapse whitespace and lowercase message body for deterministic comparison."""
    return _WHITESPACE_RE.sub(" ", body.strip().lower())


def compute_dedup_hash(
    source: str,
    sender_email: str | None,
    sender_phone: str | None,
    message_body: str,
) -> str:
    """Compute a deterministic SHA-256 fingerprint for an incoming enquiry."""
    norm_email = normalize_email(sender_email)
    norm_phone = normalize_phone(sender_phone)
    norm_msg = normalize_body(message_body)
    canonical = f"{source.strip().lower()}|{norm_email}|{norm_phone}|{norm_msg}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

