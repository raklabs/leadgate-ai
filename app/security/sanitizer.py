"""Input sanitization and OWASP LLM01 Prompt Injection detection (Constitution Section 3.1)."""

from __future__ import annotations

import re

# Strip non-printable ASCII control chars except newline (\n), carriage return (\r), and tab (\t)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Heuristic patterns targeting prompt injection, role hijacking, or score manipulation
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(previous|prior|above|system)\s+"
            r"(instructions?|prompts?|rules?|directives?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_exfiltration",
        re.compile(
            r"\b(reveal|print|output|show|repeat)\s+(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions|developer\s+message)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "score_or_tier_manipulation",
        re.compile(
            r"\b(classify|mark|score|set|tag)\s+(this\s+lead|me|this\s+enquiry)\s+as\s+(hot|100|maximum|priority|approved)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "xml_tag_breakout",
        re.compile(r"</?\s*(system|untrusted_enquiry|assistant|developer|tool_call)\s*>", re.IGNORECASE),
    ),
]


def sanitize_text(text: str | None, max_len: int = 15000) -> str:
    """Remove dangerous control characters and normalize line endings."""
    if not text:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned[:max_len]


def detect_prompt_injection(text: str) -> list[str]:
    """Scan untrusted enquiry text for adversarial prompt injection patterns.

    Returns a list of matched threat rule names (empty if clean).
    """
    if not text:
        return []
    matches: list[str] = []
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            matches.append(name)
    return matches


def wrap_untrusted_enquiry(
    source: str,
    sender_name: str | None,
    sender_email: str | None,
    sender_phone: str | None,
    subject: str | None,
    message_body: str,
) -> str:
    """Sandbox untrusted user input inside escaped XML tags so the LLM treats it strictly as data."""

    def _escape_xml_tags(val: str | None) -> str:
        if not val:
            return "(not provided)"
        # Neutralize closing/opening structural tags inside user text
        return val.replace("<", "&lt;").replace(">", "&gt;")

    return (
        "<untrusted_enquiry>\n"
        f"  <channel>{_escape_xml_tags(source)}</channel>\n"
        f"  <form_sender_name>{_escape_xml_tags(sender_name)}</form_sender_name>\n"
        f"  <form_sender_email>{_escape_xml_tags(sender_email)}</form_sender_email>\n"
        f"  <form_sender_phone>{_escape_xml_tags(sender_phone)}</form_sender_phone>\n"
        f"  <subject>{_escape_xml_tags(subject)}</subject>\n"
        f"  <message_body>\n{_escape_xml_tags(message_body)}\n  </message_body>\n"
        "</untrusted_enquiry>"
    )

