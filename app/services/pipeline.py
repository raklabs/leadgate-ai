"""End-to-end Lead Qualification & CRM Sync Orchestration Pipeline (Spec 001 Section 2)."""

from __future__ import annotations

from app.domain.dedup import compute_dedup_hash
from app.domain.models import (
    CrmSyncStatus,
    EmailDraftStatus,
    EnquiryCreateRequest,
    EnquiryStatus,
    ExtractedEnquiryData,
    LeadQualificationResponse,
)
from app.domain.scorer import RuleBasedLeadScorer
from app.domain.verifier import verify_extracted_quotes
from app.repositories.enquiry_repo import EnquiryRepository
from app.security.logging import get_logger
from app.security.sanitizer import detect_prompt_injection, sanitize_text
from app.services.crm_client import CRMClientProtocol
from app.services.email_sender import EmailSenderProtocol, LocalOutboxEmailSender
from app.services.llm_extractor import LeadExtractorProtocol

logger = get_logger(__name__)


class LeadQualificationPipeline:
    """Orchestrates persistence, AI extraction, quote verification, rule scoring, and CRM sync."""

    def __init__(
        self,
        repository: EnquiryRepository,
        extractor: LeadExtractorProtocol,
        scorer: RuleBasedLeadScorer,
        crm_client: CRMClientProtocol,
        dedup_window_hours: int = 24,
        email_sender: EmailSenderProtocol | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.scorer = scorer
        self.crm_client = crm_client
        self.dedup_window_hours = dedup_window_hours
        self.email_sender = email_sender or LocalOutboxEmailSender(db_path=repository.db_path)

    async def process_enquiry(self, request: EnquiryCreateRequest) -> LeadQualificationResponse:
        """Execute full Spec 001 qualification pipeline for an incoming enquiry."""
        # 1. Sanitize inputs
        sanitized_req = EnquiryCreateRequest(
            external_id=sanitize_text(request.external_id, max_len=128) or None,
            source=request.source,
            sender_name=sanitize_text(request.sender_name, max_len=200) or None,
            sender_email=sanitize_text(request.sender_email, max_len=254) or None,
            sender_phone=sanitize_text(request.sender_phone, max_len=64) or None,
            subject=sanitize_text(request.subject, max_len=300) or None,
            message_body=sanitize_text(request.message_body, max_len=15000),
            received_at=request.received_at,
        )

        # 2. Check Idempotency / Deduplication BEFORE incurring LLM or CRM API calls
        dedup_hash = compute_dedup_hash(
            source=sanitized_req.source.value,
            sender_email=sanitized_req.sender_email,
            sender_phone=sanitized_req.sender_phone,
            message_body=sanitized_req.message_body,
        )
        duplicate = self.repository.find_duplicate(
            dedup_hash=dedup_hash,
            external_id=sanitized_req.external_id,
            window_hours=self.dedup_window_hours,
        )
        if duplicate is not None:
            logger.info(
                f"Duplicate enquiry suppressed (enquiry_id={duplicate.enquiry_id}, "
                f"sender={sanitized_req.sender_email or 'unknown'})."
            )
            return duplicate

        # 3. Persist Raw Enquiry Immediately (Zero-Data-Loss Guarantee)
        enquiry_id = self.repository.create_raw_enquiry(
            request=sanitized_req,
            dedup_hash=dedup_hash,
        )
        logger.info(
            f"Persisted raw enquiry {enquiry_id} from {sanitized_req.sender_email or sanitized_req.sender_phone or 'anonymous'}."
        )

        review_reasons: list[str] = []

        # 4. Scan for Prompt Injection / Adversarial Manipulation
        full_source_text = f"{sanitized_req.subject or ''}\n{sanitized_req.message_body}".strip()
        injection_hits = detect_prompt_injection(full_source_text)
        if injection_hits:
            reason = f"Potential prompt injection or instruction override detected ({', '.join(injection_hits)})."
            review_reasons.append(reason)
            logger.warning(f"Security alert on {enquiry_id}: {reason}")

        # 5. Structured AI Extraction
        try:
            raw_extracted, _attempts = await self.extractor.extract(sanitized_req)
        except Exception as exc:
            logger.error(f"AI extraction failed after retries for {enquiry_id}: {exc}")
            review_reasons.append(f"AI extraction failed after retries: {exc}")
            return self.repository.update_pipeline_result(
                enquiry_id=enquiry_id,
                status=EnquiryStatus.NEEDS_REVIEW,
                needs_review=True,
                review_reasons=review_reasons,
                extracted=None,
                scoring=None,
                crm_sync=None,
                email_draft_status=EmailDraftStatus.Held_LOW_CONFIDENCE,
            )

        # 6. Verbatim Quote Verification (Enforce Zero-Guessing Policy)
        verified_extracted, quote_warnings = verify_extracted_quotes(
            extracted=raw_extracted,
            raw_source_text=full_source_text,
        )
        review_reasons.extend(quote_warnings)

        # Backfill contact details from form metadata if not present inside free text
        if not verified_extracted.contact_email and sanitized_req.sender_email:
            verified_extracted.contact_email = sanitized_req.sender_email
        if not verified_extracted.contact_phone and sanitized_req.sender_phone:
            verified_extracted.contact_phone = sanitized_req.sender_phone
        if not verified_extracted.contact_name and sanitized_req.sender_name:
            verified_extracted.contact_name = sanitized_req.sender_name

        # 7. Deterministic Rule-Based Scoring
        scoring = self.scorer.score(verified_extracted)

        # 8. Evaluate Review Conditions
        if verified_extracted.extraction_confidence < self.scorer.rules.min_confidence_threshold:
            review_reasons.append(
                f"Low AI extraction confidence ({verified_extracted.extraction_confidence:.2f} < "
                f"{self.scorer.rules.min_confidence_threshold:.2f})."
            )
        if not verified_extracted.contact_email and not verified_extracted.contact_phone:
            review_reasons.append("Missing both email and phone number; lead cannot be contacted automatically.")
        for note in verified_extracted.ambiguities_or_missing_notes:
            if "Conflicting" in note or "brief/vague" in note:
                review_reasons.append(note)

        needs_review = len(review_reasons) > 0

        # 9. Sync to CRM (with retry backoff & contact upsert deduplication)
        crm_sync = await self.crm_client.sync_lead(
            enquiry_id=enquiry_id,
            extracted=verified_extracted,
            scoring=scoring,
            needs_review=needs_review,
        )
        if crm_sync.status == CrmSyncStatus.FAILED:
            needs_review = True
            review_reasons.append(f"CRM synchronization failed after {crm_sync.attempts} attempts: {crm_sync.error_message}")

        final_status = EnquiryStatus.NEEDS_REVIEW if needs_review else EnquiryStatus.COMPLETED
        draft_status = (
            EmailDraftStatus.Held_LOW_CONFIDENCE
            if needs_review
            else EmailDraftStatus.DRAFT_PENDING_REVIEW
        )

        return self.repository.update_pipeline_result(
            enquiry_id=enquiry_id,
            status=final_status,
            needs_review=needs_review,
            review_reasons=review_reasons,
            extracted=verified_extracted,
            scoring=scoring,
            crm_sync=crm_sync,
            email_draft_status=draft_status,
        )

    async def retry_crm_sync(self, enquiry_id: str) -> LeadQualificationResponse | None:
        """Manually re-trigger CRM synchronization for an existing enquiry."""
        existing = self.repository.get_by_id(enquiry_id)
        if not existing or not existing.extracted or not existing.scoring:
            return None

        crm_sync = await self.crm_client.sync_lead(
            enquiry_id=enquiry_id,
            extracted=existing.extracted,
            scoring=existing.scoring,
            needs_review=existing.needs_review,
        )
        cleaned_reasons = [
            r for r in existing.review_reasons if not r.startswith("CRM synchronization failed")
        ]
        if crm_sync.status == CrmSyncStatus.FAILED:
            cleaned_reasons.append(
                f"CRM synchronization failed after {crm_sync.attempts} attempts: {crm_sync.error_message}"
            )

        needs_review = len(cleaned_reasons) > 0
        final_status = EnquiryStatus.NEEDS_REVIEW if needs_review else EnquiryStatus.COMPLETED

        return self.repository.update_pipeline_result(
            enquiry_id=enquiry_id,
            status=final_status,
            needs_review=needs_review,
            review_reasons=cleaned_reasons,
            extracted=existing.extracted,
            scoring=existing.scoring,
            crm_sync=crm_sync,
            email_draft_status=existing.email_draft_status,
        )

    async def approve_and_send_email(
        self,
        enquiry_id: str,
        reviewer_name: str,
        subject: str | None = None,
        body: str | None = None,
    ) -> LeadQualificationResponse | None:
        """Approve (and optionally edit) the drafted acknowledgement email and dispatch it via EmailSender."""
        updated = self.repository.approve_email_draft(
            enquiry_id=enquiry_id,
            reviewer_name=reviewer_name,
            subject=subject,
            body=body,
        )
        if not updated:
            return None

        if updated.extracted and updated.extracted.contact_email:
            await self.email_sender.send_approved_email(
                enquiry_id=enquiry_id,
                to_email=updated.extracted.contact_email,
                subject=updated.extracted.draft_acknowledgement_subject,
                body=updated.extracted.draft_acknowledgement_body,
                approved_by=reviewer_name,
            )
        return updated

