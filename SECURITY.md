# Security Policy & Architecture Controls

> **Maintained by**: IntelligentStack AI (`raklabs`)

We take the security of AI pipelines and customer CRM integrations seriously. This repository implements defense-in-depth controls aligned with the **OWASP Top 10 for Large Language Model Applications** and the **OWASP API Security Top 10**.

---

## 1. Active Security Controls

| Control Area | Implementation | Threat Mitigated |
| :--- | :--- | :--- |
| **Prompt Injection & Untrusted Data Sandboxing** | [`app/security/sanitizer.py`](app/security/sanitizer.py) wraps all user input inside escaped `<untrusted_enquiry>` XML tags and runs `detect_prompt_injection` heuristics before LLM calls. | **OWASP LLM01 (Prompt Injection)** — Prevents attackers from overriding system rules or manipulating lead scores. |
| **Verbatim Quote Anti-Hallucination Verifier** | [`app/domain/verifier.py`](app/domain/verifier.py) checks in pure Python that extracted budgets, timelines, and services appear verbatim in the source text; unverified fields are stripped to `None`. | **OWASP LLM09 (Overreliance / Hallucination)** — Guarantees zero fabricated numbers enter the CRM. |
| **Human-in-the-Loop Outbound Email Gate** | [`app/services/email_sender.py`](app/services/email_sender.py) & [`app/services/pipeline.py`](app/services/pipeline.py) hold all AI-drafted emails in `DRAFT_PENDING_REVIEW` until explicitly approved by an authenticated operator. | **OWASP LLM08 (Excessive Agency)** — Prevents unreviewed outbound emails from being sent to customers. |
| **Constant-Time Webhook HMAC & API Key Auth** | [`app/security/auth.py`](app/security/auth.py) verifies `X-Webhook-Signature` (HMAC-SHA256) with a `±300s` timestamp replay window and `X-API-Key` using `hmac.compare_digest`. | **OWASP API2 (Broken Authentication)** — Prevents timing attacks, webhook spoofing, and replay attacks. |
| **Sliding-Window Rate Limiting & Payload Caps** | [`app/security/rate_limit.py`](app/security/rate_limit.py) enforces per-client request limits (`429 Too Many Requests` + `Retry-After`) and a `64 KB` payload cap before LLM invocation. | **OWASP LLM04 (Model Denial of Service / Denial of Wallet)** — Protects against token-exhaustion floods. |
| **Automatic PII Redaction in Logs** | [`app/security/logging.py`](app/security/logging.py) masks email addresses (`v***@domain.com`) and phone numbers (`***-***-0192`) across all JSON log streams and stack traces. | **OWASP LLM06 (Sensitive Information Disclosure)** — Prevents GDPR/PII leakage into log aggregators. |
| **Parameterized Database & CRM Queries** | [`app/repositories/enquiry_repo.py`](app/repositories/enquiry_repo.py) uses parameterized SQLite statements (`?`) and Salesforce `parameterizedSearch`. | **Injection (SQL / SOQL)** — Eliminates query injection vectors. |

---

## 2. Reporting a Vulnerability

If you discover a security issue in this repository, please report it privately by opening a security advisory on GitHub (`github.com/raklabs`) rather than filing a public issue with exploit details.

