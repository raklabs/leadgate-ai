"""Domain models and strict Pydantic v2 contracts (Spec 001 Section 3)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnquirySource(StrEnum):
    WEB_FORM = "web_form"
    EMAIL = "email"


class ServiceCategory(StrEnum):
    RESIDENTIAL_SALES = "residential_sales"
    COMMERCIAL_LEASE = "commercial_lease"
    PROPERTY_MANAGEMENT = "property_management"
    ADVISORY_CONSULTING = "advisory_consulting"
    RENOVATION_FITOUT = "renovation_fitout"
    OTHER = "other"


class TimelineBucket(StrEnum):
    IMMEDIATE = "immediate"
    SHORT_TERM = "short_term"
    MEDIUM_TERM = "medium_term"
    LONG_TERM = "long_term"
    EXPLORATORY = "exploratory"


class UrgencyLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LeadTier(StrEnum):
    HOT = "Hot"
    WARM = "Warm"
    MEDIUM = "Medium"
    COLD = "Cold"


class EnquiryStatus(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    SCORING = "SCORING"
    SYNCING_CRM = "SYNCING_CRM"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class CrmSyncStatus(StrEnum):
    PENDING = "PENDING"
    SYNCED = "SYNCED"
    UPDATED_EXISTING = "UPDATED_EXISTING"
    FAILED = "FAILED"
    SKIPPED_SPAM = "SKIPPED_SPAM"


class EmailDraftStatus(StrEnum):
    DRAFT_PENDING_REVIEW = "DRAFT_PENDING_REVIEW"
    APPROVED_AND_QUEUED = "APPROVED_AND_QUEUED"
    Held_LOW_CONFIDENCE = "HELD_LOW_CONFIDENCE"


class EnquiryCreateRequest(BaseModel):
    """Inbound payload from web contact form or inbound email webhook."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_id: str | None = Field(default=None, max_length=128)
    source: EnquirySource = Field(default=EnquirySource.WEB_FORM)
    sender_name: str | None = Field(default=None, max_length=200)
    sender_email: str | None = Field(default=None, max_length=254)
    sender_phone: str | None = Field(default=None, max_length=64)
    subject: str | None = Field(default=None, max_length=300)
    message_body: str = Field(..., min_length=1, max_length=15000)
    received_at: datetime | None = None


class ExtractedEnquiryData(BaseModel):
    """Structured output contract returned by the AI extractor.

    Every factual attribute is nullable (None by default) to enforce the Zero-Guessing Policy.
    """

    model_config = ConfigDict(extra="forbid")

    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    company_name: str | None = None

    service_or_property_type: ServiceCategory | None = None
    service_evidence_quote: str | None = None

    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    budget_currency: str | None = Field(default=None, max_length=8)
    budget_evidence_quote: str | None = None

    timeline: TimelineBucket | None = None
    timeline_evidence_quote: str | None = None

    urgency: UrgencyLevel | None = None
    location: str | None = None

    ai_summary: str = Field(..., max_length=600)
    extraction_confidence: float = Field(..., ge=0.0, le=1.0)
    ambiguities_or_missing_notes: list[str] = Field(default_factory=list)
    is_spam_or_irrelevant: bool = False

    draft_acknowledgement_subject: str = Field(..., max_length=200)
    draft_acknowledgement_body: str = Field(..., max_length=2500)


class ScoreComponent(BaseModel):
    """Individual rule dimension contribution to total score."""

    dimension: str
    points: int
    reason: str


class ScoreBreakdown(BaseModel):
    """Deterministic scoring engine output."""

    total_score: int = Field(..., ge=0, le=100)
    tier: LeadTier
    rule_version: str
    components: list[ScoreComponent]


class CrmSyncResult(BaseModel):
    """Outcome of syncing the qualified lead to the target CRM."""

    provider: str
    status: CrmSyncStatus
    crm_record_id: str | None = None
    is_existing_contact: bool = False
    attempts: int = 1
    error_message: str | None = None


class EmailApprovalRequest(BaseModel):
    """Operator request to approve (and optionally edit) the drafted acknowledgement email."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reviewer_name: str = Field(..., min_length=1, max_length=120)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=2500)


class LeadQualificationResponse(BaseModel):
    """Complete API response for a processed enquiry."""

    enquiry_id: str
    external_id: str | None
    is_duplicate: bool = False
    source: EnquirySource
    status: EnquiryStatus
    needs_review: bool
    review_reasons: list[str]
    extracted: ExtractedEnquiryData | None = None
    scoring: ScoreBreakdown | None = None
    crm_sync: CrmSyncResult | None = None
    email_draft_status: EmailDraftStatus
    approved_by: str | None = None
    created_at: str
    updated_at: str

