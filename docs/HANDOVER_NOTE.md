# Technical Handover Note — Phase 1 AI Lead Qualification

> **Prepared by**: IntelligentStack AI (`raklabs`)  
> **Specification**: [specs/001-ai-lead-qualification.md](../specs/001-ai-lead-qualification.md)  
> **Architecture Standard**: [CONSTITUTION.md](../CONSTITUTION.md)

---

## 1. Executive Summary

This module integrates structured AI lead extraction, configurable rule-based scoring (`Hot` / `Warm` / `Medium` / `Cold`), resilient CRM synchronization (**Salesforce**, **HubSpot**, **GoHighLevel**), and human-reviewed acknowledgement email drafting into an existing Python backend.

### Core Production Guarantees
1. **Zero Lost Leads (`Persist-Before-Process`)**: Every incoming web form or email webhook is persisted to durable storage (`status="RECEIVED"`) *before* any LLM or CRM network call is made.
2. **Zero Hallucinated Facts (`Verbatim Quote Verifier`)**: Every extracted commercial attribute (`budget_min`, `budget_max`, `timeline`, `service_or_property_type`) is nullable (`None` by default) and requires a verbatim `*_evidence_quote` verified against the raw text in Python ([`app/domain/verifier.py`](../app/domain/verifier.py)).
3. **100% Deterministic Business Scoring**: The AI never decides lead scores. [`RuleBasedLeadScorer`](../app/domain/scorer.py) computes scores and tiers strictly from [`config/scoring_rules.yaml`](../config/scoring_rules.yaml).
4. **No Duplicate Leads**: Duplicate form submissions within the deduplication window (default 24h) are suppressed via SHA-256 fingerprinting before calling the LLM; repeat enquiries from existing customers update their existing CRM record (`UPDATED_EXISTING`).
5. **Human-in-the-Loop Email Safety**: AI-drafted acknowledgement emails are held in `DRAFT_PENDING_REVIEW` (or `HELD_LOW_CONFIDENCE`) until approved via `/dashboard` or `POST /api/v1/enquiries/{id}/approve-email`.

---

## 2. Integrating Into Your Existing Python Backend

### Option A: Existing FastAPI Application
Mount the qualification endpoints or call `get_pipeline().process_enquiry(...)` directly inside your existing contact form route:

```python
from app.domain.models import EnquiryCreateRequest, EnquirySource
from app.main import get_pipeline

async def handle_contact_form_submission(form_data: dict):
    pipeline = get_pipeline()
    result = await pipeline.process_enquiry(
        EnquiryCreateRequest(
            external_id=form_data.get("submission_id"),
            source=EnquirySource.WEB_FORM,
            sender_name=form_data.get("name"),
            sender_email=form_data.get("email"),
            sender_phone=form_data.get("phone"),
            subject=form_data.get("subject"),
            message_body=form_data["message"],
        )
    )
    return result
```

### Option B: Existing Django Application (Views / Signals / Celery)
Call the pipeline synchronously or inside a Celery task from any Django view or `post_save` signal:

```python
import asyncio
from app.domain.models import EnquiryCreateRequest, EnquirySource
from app.main import get_pipeline

def on_django_enquiry_saved(enquiry_model_instance):
    pipeline = get_pipeline()
    request = EnquiryCreateRequest(
        external_id=str(enquiry_model_instance.id),
        source=EnquirySource.WEB_FORM,
        sender_name=enquiry_model_instance.full_name,
        sender_email=enquiry_model_instance.email,
        sender_phone=enquiry_model_instance.phone,
        subject=enquiry_model_instance.subject,
        message_body=enquiry_model_instance.body,
    )
    return asyncio.run(pipeline.process_enquiry(request))
```

---

## 3. Day-to-Day Configuration Guide

### 3.1 Changing Scoring Weights & Thresholds
Open [`config/scoring_rules.yaml`](../config/scoring_rules.yaml):
- Adjust `tier_thresholds` (`hot: 75`, `warm: 50`, `medium: 25`) to change what qualifies as `Hot` vs `Warm`.
- Adjust `service_weights`, `budget_brackets`, `timeline_weights`, or `urgency_weights` to reflect changing business priorities.
- Adjust `min_confidence_threshold` (default `0.75`) to tune when leads are flagged `needs_review = True`.

### 3.2 Switching AI & CRM Providers (`.env`)
| Setting | Options | Description |
| :--- | :--- | :--- |
| `LLM_PROVIDER` | `ollama`, `openai`, `openai_compatible`, `anthropic`, `deterministic_local` | Active AI extraction backend |
| `CRM_PROVIDER` | `mock`, `hubspot`, `gohighlevel`, `salesforce` | Target CRM adapter |
| `EMAIL_PROVIDER` | `local_outbox`, `resend`, `smtp` | Outbound email delivery channel upon operator approval |
| `REQUIRE_WEBHOOK_SIGNATURE` | `true` / `false` | Require `X-Webhook-Signature` HMAC-SHA256 verification on inbound webhooks |

---

## 4. Handling Failures & Retries
- If the CRM API returns `429` or `5xx`, [`execute_with_retry`](../app/services/retry.py) automatically retries up to 3 times with exponential backoff and jitter.
- If the CRM remains unreachable after 3 attempts, the lead is saved with `status="NEEDS_REVIEW"` and `crm_sync.status="FAILED"`.
- Operators can re-trigger CRM sync with one click in `/dashboard` (**Retry CRM Sync**) or via `POST /api/v1/enquiries/{enquiry_id}/retry-sync`.

