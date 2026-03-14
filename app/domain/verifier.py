"""Verbatim Evidence Quote Verifier enforcing the Zero-Guessing Policy (Spec 001 Section 2.1)."""

from __future__ import annotations

import re

from app.domain.models import ExtractedEnquiryData

_WS_RE = re.compile(r"\s+")
_HAS_NUMBER_OR_BUDGET_WORD_RE = re.compile(r"(\d|thousand|million|k\b|m\b|budget)", re.IGNORECASE)


def _quote_exists_in_source(quote: str | None, source_text: str) -> bool:
    """Check whether the evidence quote appears in the source text (case & whitespace normalized)."""
    if not quote or not quote.strip():
        return False
    norm_quote = _WS_RE.sub(" ", quote.strip().lower())
    norm_source = _WS_RE.sub(" ", source_text.strip().lower())
    if len(norm_quote) < 2:
        return False
    return norm_quote in norm_source


def _budget_number_exists_in_source(amount: float | None, source_text: str) -> str | None:
    """Check if the numeric budget amount (e.g. 85000, 85,000, or 85k) actually appears in source_text."""
    if amount is None or amount <= 0:
        return None
    int_val = int(round(amount))
    candidates = [
        f"{int_val:,}",
        str(int_val),
    ]
    if int_val >= 1000 and int_val % 1000 == 0:
        k_val = int_val // 1000
        candidates.extend([f"{k_val}k", f"{k_val}K", f"{k_val} thousand"])
    norm_source = source_text.lower()
    for cand in candidates:
        if cand.lower() in norm_source:
            return cand
    return None


def verify_extracted_quotes(
    extracted: ExtractedEnquiryData,
    raw_source_text: str,
) -> tuple[ExtractedEnquiryData, list[str]]:
    """Verify that extracted commercial fields have verbatim evidence in the raw enquiry.

    Any field lacking valid verbatim evidence in raw_source_text is stripped to None to prevent AI guessing.
    Returns (sanitized_extraction, verification_warnings).
    """
    warnings: list[str] = []
    data = extracted.model_copy(deep=True)

    # 1. Verify Budget Evidence
    if data.budget_min is not None or data.budget_max is not None:
        quote_ok = _quote_exists_in_source(data.budget_evidence_quote, raw_source_text)
        has_num = bool(data.budget_evidence_quote and _HAS_NUMBER_OR_BUDGET_WORD_RE.search(data.budget_evidence_quote))
        matched_num_quote = _budget_number_exists_in_source(data.budget_max or data.budget_min, raw_source_text)
        if quote_ok and has_num:
            pass
        elif matched_num_quote is not None:
            # Grounded number exists verbatim in source text even if small LLM omitted quote field
            data.budget_evidence_quote = matched_num_quote
        else:
            warnings.append(
                "Stripped unverified budget value: evidence quote missing or not found verbatim in enquiry."
            )
            data.budget_min = None
            data.budget_max = None
            data.budget_currency = None
            data.budget_evidence_quote = None
            data.extraction_confidence = min(data.extraction_confidence, 0.55)

    # 2. Verify Timeline Evidence
    if data.timeline is not None:
        if not _quote_exists_in_source(data.timeline_evidence_quote, raw_source_text):
            warnings.append(
                "Stripped unverified timeline value: evidence quote missing or not found verbatim in enquiry."
            )
            data.timeline = None
            data.timeline_evidence_quote = None
            data.extraction_confidence = min(data.extraction_confidence, 0.60)

    # 3. Verify Service / Property Type Evidence
    if data.service_or_property_type is not None:
        if not _quote_exists_in_source(data.service_evidence_quote, raw_source_text):
            warnings.append(
                "Stripped unverified service_or_property_type: evidence quote missing or not found verbatim in enquiry."
            )
            data.service_or_property_type = None
            data.service_evidence_quote = None
            data.extraction_confidence = min(data.extraction_confidence, 0.60)

    return data, warnings

