## Summary

- **Related Specification**: `specs/001-ai-lead-qualification.md` (or link new spec)
- **Objective**: Briefly describe what this PR implements or fixes.

---

## Spec-Driven Development & Architecture Checklist

- [ ] **Specification Traceability**: Changes correspond to an approved section/acceptance criterion in `specs/`.
- [ ] **Zero-Data-Loss (`Persist-Before-Process`)**: Raw inbound payloads are persisted to durable storage before any external network call (LLM or CRM).
- [ ] **Zero-Guessing Policy**: All extracted fields remain nullable (`Optional`), and commercial fields require verbatim evidence quotes verified in Python.
- [ ] **Deterministic Business Logic**: Lead scoring and tier assignment remain strictly rule-based (`config/scoring_rules.yaml`), never delegated to LLM judgment.
- [ ] **Idempotency & Retries**: Duplicate webhooks are suppressed via SHA-256 fingerprint; external HTTP calls use explicit timeouts and exponential backoff with jitter.

---

## Security Review Checklist (OWASP API & LLM Top 10)

- [ ] **Prompt Injection Isolation (`LLM01`)**: Untrusted user input is wrapped inside escaped `<untrusted_enquiry>` tags and scanned via `detect_prompt_injection`.
- [ ] **Human-in-the-Loop Outbound Safety (`LLM02` / `LLM08`)**: No outbound emails are dispatched automatically without explicit operator approval.
- [ ] **Webhook & API Authentication**: Protected endpoints enforce constant-time `hmac.compare_digest` (`X-API-Key` or `X-Webhook-Signature` + replay window).
- [ ] **PII & Secrets Hygiene**: Zero hardcoded credentials (`SecretStr` used in `app/config.py`), and structured logs mask emails/phone numbers via `redact_pii`.

---

## Verification Evidence

```bash
.venv/bin/pytest
.venv/bin/python scripts/evaluate_20_enquiries.py --provider deterministic_local
```

