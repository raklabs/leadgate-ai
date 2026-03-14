"""Transactional SQLite repository implementing Persist-Before-Process (Constitution Section 2.1)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.models import (
    CrmSyncResult,
    EmailDraftStatus,
    EnquiryCreateRequest,
    EnquirySource,
    EnquiryStatus,
    ExtractedEnquiryData,
    LeadQualificationResponse,
    ScoreBreakdown,
)


class EnquiryRepository:
    """Durable SQLite persistence with WAL mode for enquiries, deduplication, and mock CRM records."""

    def __init__(self, db_path: str = "data/lead_qualifier.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS enquiries (
                    enquiry_id TEXT PRIMARY KEY,
                    external_id TEXT,
                    dedup_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sender_name TEXT,
                    sender_email TEXT,
                    sender_phone TEXT,
                    subject TEXT,
                    message_body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    review_reasons_json TEXT NOT NULL DEFAULT '[]',
                    extracted_json TEXT,
                    scoring_json TEXT,
                    crm_sync_json TEXT,
                    email_draft_status TEXT NOT NULL DEFAULT 'DRAFT_PENDING_REVIEW',
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_enquiries_dedup_hash
                    ON enquiries(dedup_hash, created_at);

                CREATE INDEX IF NOT EXISTS idx_enquiries_external_id
                    ON enquiries(external_id);

                CREATE TABLE IF NOT EXISTS mock_crm_contacts (
                    crm_record_id TEXT PRIMARY KEY,
                    email TEXT,
                    phone TEXT,
                    contact_name TEXT,
                    company_name TEXT,
                    lead_tier TEXT,
                    lead_score INTEGER,
                    ai_summary TEXT,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    notes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def find_duplicate(
        self,
        dedup_hash: str,
        external_id: str | None,
        window_hours: int = 24,
    ) -> LeadQualificationResponse | None:
        """Check if an identical enquiry or external_id was already ingested within the time window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        with self._connect() as conn:
            if external_id:
                row = conn.execute(
                    "SELECT * FROM enquiries WHERE external_id = ? ORDER BY created_at DESC LIMIT 1",
                    (external_id,),
                ).fetchone()
                if row:
                    resp = self._row_to_response(row)
                    resp.is_duplicate = True
                    return resp

            row = conn.execute(
                "SELECT * FROM enquiries WHERE dedup_hash = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (dedup_hash, cutoff),
            ).fetchone()
            if row:
                resp = self._row_to_response(row)
                resp.is_duplicate = True
                return resp
        return None

    def create_raw_enquiry(
        self,
        request: EnquiryCreateRequest,
        dedup_hash: str,
    ) -> str:
        """Persist raw enquiry BEFORE any external LLM or CRM call (Zero-Data-Loss guarantee)."""
        enquiry_id = f"enq_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enquiries (
                    enquiry_id, external_id, dedup_hash, source,
                    sender_name, sender_email, sender_phone, subject, message_body,
                    status, needs_review, review_reasons_json, email_draft_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', ?, ?, ?)
                """,
                (
                    enquiry_id,
                    request.external_id,
                    dedup_hash,
                    request.source.value,
                    request.sender_name,
                    request.sender_email,
                    request.sender_phone,
                    request.subject,
                    request.message_body,
                    EnquiryStatus.RECEIVED.value,
                    EmailDraftStatus.DRAFT_PENDING_REVIEW.value,
                    now,
                    now,
                ),
            )
        return enquiry_id

    def update_pipeline_result(
        self,
        enquiry_id: str,
        status: EnquiryStatus,
        needs_review: bool,
        review_reasons: list[str],
        extracted: ExtractedEnquiryData | None,
        scoring: ScoreBreakdown | None,
        crm_sync: CrmSyncResult | None,
        email_draft_status: EmailDraftStatus,
    ) -> LeadQualificationResponse:
        """Save extraction, scoring, review flags, and CRM sync results for an enquiry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE enquiries
                SET status = ?,
                    needs_review = ?,
                    review_reasons_json = ?,
                    extracted_json = ?,
                    scoring_json = ?,
                    crm_sync_json = ?,
                    email_draft_status = ?,
                    updated_at = ?
                WHERE enquiry_id = ?
                """,
                (
                    status.value,
                    1 if needs_review else 0,
                    json.dumps(review_reasons),
                    extracted.model_dump_json() if extracted else None,
                    scoring.model_dump_json() if scoring else None,
                    crm_sync.model_dump_json() if crm_sync else None,
                    email_draft_status.value,
                    now,
                    enquiry_id,
                ),
            )
            row = conn.execute("SELECT * FROM enquiries WHERE enquiry_id = ?", (enquiry_id,)).fetchone()
            if not row:
                raise KeyError(f"Enquiry {enquiry_id} not found after update.")
            return self._row_to_response(row)

    def get_by_id(self, enquiry_id: str) -> LeadQualificationResponse | None:
        """Retrieve a processed enquiry by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM enquiries WHERE enquiry_id = ?", (enquiry_id,)).fetchone()
            return self._row_to_response(row) if row else None

    def get_raw_fields(self, enquiry_id: str) -> dict[str, Any] | None:
        """Retrieve raw input fields for re-running extraction or CRM sync."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM enquiries WHERE enquiry_id = ?", (enquiry_id,)).fetchone()
            return dict(row) if row else None

    def list_enquiries(
        self,
        tier: str | None = None,
        needs_review: bool | None = None,
        limit: int = 100,
    ) -> list[LeadQualificationResponse]:
        """List processed enquiries with optional filtering by tier and needs_review flag."""
        query = "SELECT * FROM enquiries WHERE 1=1"
        params: list[Any] = []
        if needs_review is not None:
            query += " AND needs_review = ?"
            params.append(1 if needs_review else 0)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        results = [self._row_to_response(r) for r in rows]
        if tier:
            results = [r for r in results if r.scoring and r.scoring.tier.value.lower() == tier.lower()]
        return results

    def approve_email_draft(
        self,
        enquiry_id: str,
        reviewer_name: str,
        subject: str | None = None,
        body: str | None = None,
    ) -> LeadQualificationResponse | None:
        """Approve (and optionally update) the drafted acknowledgement email."""
        existing = self.get_by_id(enquiry_id)
        if not existing:
            return None

        extracted = existing.extracted
        if extracted:
            if subject:
                extracted.draft_acknowledgement_subject = subject
            if body:
                extracted.draft_acknowledgement_body = body

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE enquiries
                SET extracted_json = ?,
                    email_draft_status = ?,
                    approved_by = ?,
                    updated_at = ?
                WHERE enquiry_id = ?
                """,
                (
                    extracted.model_dump_json() if extracted else None,
                    EmailDraftStatus.APPROVED_AND_QUEUED.value,
                    reviewer_name,
                    now,
                    enquiry_id,
                ),
            )
            row = conn.execute("SELECT * FROM enquiries WHERE enquiry_id = ?", (enquiry_id,)).fetchone()
            return self._row_to_response(row) if row else None

    def _row_to_response(self, row: sqlite3.Row) -> LeadQualificationResponse:
        extracted = (
            ExtractedEnquiryData.model_validate_json(row["extracted_json"])
            if row["extracted_json"]
            else None
        )
        scoring = (
            ScoreBreakdown.model_validate_json(row["scoring_json"])
            if row["scoring_json"]
            else None
        )
        crm_sync = (
            CrmSyncResult.model_validate_json(row["crm_sync_json"])
            if row["crm_sync_json"]
            else None
        )
        return LeadQualificationResponse(
            enquiry_id=row["enquiry_id"],
            external_id=row["external_id"],
            is_duplicate=False,
            source=EnquirySource(row["source"]),
            status=EnquiryStatus(row["status"]),
            needs_review=bool(row["needs_review"]),
            review_reasons=json.loads(row["review_reasons_json"] or "[]"),
            extracted=extracted,
            scoring=scoring,
            crm_sync=crm_sync,
            email_draft_status=EmailDraftStatus(row["email_draft_status"]),
            approved_by=row["approved_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

