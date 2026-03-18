"""Multi-provider CRM Adapter (HubSpot, GoHighLevel, and Local Stateful Mock) with retry & upsert deduplication."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Protocol

import httpx

from app.config import Settings
from app.domain.dedup import normalize_email, normalize_phone
from app.domain.models import (
    CrmSyncResult,
    CrmSyncStatus,
    ExtractedEnquiryData,
    ScoreBreakdown,
)
from app.services.retry import TRANSIENT_STATUS_CODES, TransientAPIError, execute_with_retry


class CRMClientProtocol(Protocol):
    """Interface for syncing qualified leads to an external or local CRM."""

    async def sync_lead(
        self,
        enquiry_id: str,
        extracted: ExtractedEnquiryData,
        scoring: ScoreBreakdown,
        needs_review: bool,
    ) -> CrmSyncResult:
        ...


class MockCRMClient:
    """Local SQLite-backed CRM adapter supporting contact deduplication and fault-injection testing."""

    def __init__(
        self,
        db_path: str = "data/lead_qualifier.db",
        max_retries: int = 3,
        fail_first_n_attempts: int = 0,
    ) -> None:
        self.db_path = db_path
        self.max_retries = max_retries
        self.fail_first_n_attempts = fail_first_n_attempts
        self._attempt_counter: dict[str, int] = {}

    async def sync_lead(
        self,
        enquiry_id: str,
        extracted: ExtractedEnquiryData,
        scoring: ScoreBreakdown,
        needs_review: bool,
    ) -> CrmSyncResult:
        if extracted.is_spam_or_irrelevant:
            return CrmSyncResult(
                provider="mock",
                status=CrmSyncStatus.SKIPPED_SPAM,
                crm_record_id=None,
                is_existing_contact=False,
                attempts=1,
            )

        async def _upsert_sqlite() -> tuple[str, bool]:
            count = self._attempt_counter.get(enquiry_id, 0) + 1
            self._attempt_counter[enquiry_id] = count
            if count <= self.fail_first_n_attempts:
                raise TransientAPIError(
                    f"Simulated CRM 503 Service Unavailable on attempt {count}",
                    status_code=503,
                )

            norm_email = normalize_email(extracted.contact_email)
            norm_phone = normalize_phone(extracted.contact_phone)
            now = datetime.now(timezone.utc).isoformat()

            note_entry = {
                "enquiry_id": enquiry_id,
                "synced_at": now,
                "score": scoring.total_score,
                "tier": scoring.tier.value,
                "summary": extracted.ai_summary,
                "needs_review": needs_review,
            }

            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                existing = None
                if norm_email:
                    existing = conn.execute(
                        "SELECT * FROM mock_crm_contacts WHERE email = ? LIMIT 1",
                        (norm_email,),
                    ).fetchone()
                if not existing and norm_phone:
                    existing = conn.execute(
                        "SELECT * FROM mock_crm_contacts WHERE phone = ? LIMIT 1",
                        (norm_phone,),
                    ).fetchone()

                if existing:
                    crm_id = existing["crm_record_id"]
                    notes = json.loads(existing["notes_json"] or "[]")
                    notes.append(note_entry)
                    conn.execute(
                        """
                        UPDATE mock_crm_contacts
                        SET contact_name = COALESCE(?, contact_name),
                            company_name = COALESCE(?, company_name),
                            lead_tier = ?,
                            lead_score = ?,
                            ai_summary = ?,
                            needs_review = ?,
                            notes_json = ?,
                            updated_at = ?
                        WHERE crm_record_id = ?
                        """,
                        (
                            extracted.contact_name,
                            extracted.company_name,
                            scoring.tier.value,
                            scoring.total_score,
                            extracted.ai_summary,
                            1 if needs_review else 0,
                            json.dumps(notes),
                            now,
                            crm_id,
                        ),
                    )
                    return crm_id, True

                crm_id = f"crm_contact_{uuid.uuid4().hex[:10]}"
                conn.execute(
                    """
                    INSERT INTO mock_crm_contacts (
                        crm_record_id, email, phone, contact_name, company_name,
                        lead_tier, lead_score, ai_summary, needs_review, notes_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        crm_id,
                        norm_email or None,
                        norm_phone or None,
                        extracted.contact_name,
                        extracted.company_name,
                        scoring.tier.value,
                        scoring.total_score,
                        extracted.ai_summary,
                        1 if needs_review else 0,
                        json.dumps([note_entry]),
                        now,
                        now,
                    ),
                )
                return crm_id, False

        try:
            (crm_id, is_existing), attempts = await execute_with_retry(
                operation_name="mock_crm_upsert",
                func=_upsert_sqlite,
                max_retries=self.max_retries,
            )
            return CrmSyncResult(
                provider="mock",
                status=CrmSyncStatus.UPDATED_EXISTING if is_existing else CrmSyncStatus.SYNCED,
                crm_record_id=crm_id,
                is_existing_contact=is_existing,
                attempts=attempts,
            )
        except Exception as exc:
            attempts = self._attempt_counter.get(enquiry_id, self.max_retries)
            return CrmSyncResult(
                provider="mock",
                status=CrmSyncStatus.FAILED,
                crm_record_id=None,
                is_existing_contact=False,
                attempts=attempts,
                error_message=str(exc),
            )


class HubSpotCRMClient:
    """HubSpot CRM v3 REST API adapter with contact search deduplication and retry backoff."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.CRM_API_KEY:
            raise ValueError("CRM_API_KEY is required when CRM_PROVIDER='hubspot'.")
        self.token = settings.CRM_API_KEY.get_secret_value()
        self.base_url = settings.CRM_BASE_URL.rstrip("/")

    async def sync_lead(
        self,
        enquiry_id: str,
        extracted: ExtractedEnquiryData,
        scoring: ScoreBreakdown,
        needs_review: bool,
    ) -> CrmSyncResult:
        if extracted.is_spam_or_irrelevant:
            return CrmSyncResult(
                provider="hubspot",
                status=CrmSyncStatus.SKIPPED_SPAM,
                attempts=1,
            )

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        properties = {
            "email": normalize_email(extracted.contact_email),
            "phone": extracted.contact_phone or "",
            "firstname": (extracted.contact_name or "").split(" ")[0],
            "lastname": " ".join((extracted.contact_name or "").split(" ")[1:]),
            "company": extracted.company_name or "",
            "hs_lead_status": scoring.tier.value.upper(),
            "ai_lead_score": str(scoring.total_score),
            "ai_lead_summary": extracted.ai_summary,
            "ai_needs_review": "true" if needs_review else "false",
        }

        async def _hubspot_upsert() -> tuple[str, bool]:
            timeout = httpx.Timeout(self.settings.CRM_TIMEOUT_SECONDS, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                existing_id: str | None = None
                if properties["email"]:
                    search_resp = await client.post(
                        f"{self.base_url}/crm/v3/objects/contacts/search",
                        headers=headers,
                        json={
                            "filterGroups": [
                                {
                                    "filters": [
                                        {
                                            "propertyName": "email",
                                            "operator": "EQ",
                                            "value": properties["email"],
                                        }
                                    ]
                                }
                            ]
                        },
                    )
                    if search_resp.status_code in TRANSIENT_STATUS_CODES:
                        raise TransientAPIError(
                            f"HubSpot search HTTP {search_resp.status_code}",
                            status_code=search_resp.status_code,
                        )
                    search_resp.raise_for_status()
                    results = search_resp.json().get("results", [])
                    if results:
                        existing_id = str(results[0]["id"])

                if existing_id:
                    patch_resp = await client.patch(
                        f"{self.base_url}/crm/v3/objects/contacts/{existing_id}",
                        headers=headers,
                        json={"properties": properties},
                    )
                    if patch_resp.status_code in TRANSIENT_STATUS_CODES:
                        raise TransientAPIError(
                            f"HubSpot update HTTP {patch_resp.status_code}",
                            status_code=patch_resp.status_code,
                        )
                    patch_resp.raise_for_status()
                    return existing_id, True

                create_resp = await client.post(
                    f"{self.base_url}/crm/v3/objects/contacts",
                    headers=headers,
                    json={"properties": properties},
                )
                if create_resp.status_code in TRANSIENT_STATUS_CODES:
                    raise TransientAPIError(
                        f"HubSpot create HTTP {create_resp.status_code}",
                        status_code=create_resp.status_code,
                    )
                create_resp.raise_for_status()
                new_id = str(create_resp.json()["id"])
                return new_id, False

        try:
            (crm_id, is_existing), attempts = await execute_with_retry(
                operation_name="hubspot_crm_upsert",
                func=_hubspot_upsert,
                max_retries=self.settings.CRM_MAX_RETRIES,
            )
            return CrmSyncResult(
                provider="hubspot",
                status=CrmSyncStatus.UPDATED_EXISTING if is_existing else CrmSyncStatus.SYNCED,
                crm_record_id=crm_id,
                is_existing_contact=is_existing,
                attempts=attempts,
            )
        except Exception as exc:
            return CrmSyncResult(
                provider="hubspot",
                status=CrmSyncStatus.FAILED,
                attempts=self.settings.CRM_MAX_RETRIES,
                error_message=str(exc),
            )


class GoHighLevelCRMClient:
    """GoHighLevel (GHL) REST API adapter using /contacts/upsert."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.CRM_API_KEY:
            raise ValueError("CRM_API_KEY is required when CRM_PROVIDER='gohighlevel'.")
        self.token = settings.CRM_API_KEY.get_secret_value()
        self.base_url = settings.CRM_BASE_URL.rstrip("/")

    async def sync_lead(
        self,
        enquiry_id: str,
        extracted: ExtractedEnquiryData,
        scoring: ScoreBreakdown,
        needs_review: bool,
    ) -> CrmSyncResult:
        if extracted.is_spam_or_irrelevant:
            return CrmSyncResult(
                provider="gohighlevel",
                status=CrmSyncStatus.SKIPPED_SPAM,
                attempts=1,
            )

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
        }
        tags = [f"Tier:{scoring.tier.value}", f"Score:{scoring.total_score}"]
        if needs_review:
            tags.append("NeedsReview")

        payload = {
            "name": extracted.contact_name,
            "email": normalize_email(extracted.contact_email) or None,
            "phone": extracted.contact_phone,
            "companyName": extracted.company_name,
            "tags": tags,
            "customFields": [
                {"key": "ai_summary", "field_value": extracted.ai_summary},
                {"key": "lead_score", "field_value": scoring.total_score},
            ],
        }

        async def _ghl_upsert() -> tuple[str, bool]:
            timeout = httpx.Timeout(self.settings.CRM_TIMEOUT_SECONDS, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/contacts/upsert",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    raise TransientAPIError(
                        f"GoHighLevel HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )
                resp.raise_for_status()
                body = resp.json()
                contact = body.get("contact", {})
                return str(contact.get("id", f"ghl_{enquiry_id}")), bool(body.get("new") is False)

        try:
            (crm_id, is_existing), attempts = await execute_with_retry(
                operation_name="ghl_crm_upsert",
                func=_ghl_upsert,
                max_retries=self.settings.CRM_MAX_RETRIES,
            )
            return CrmSyncResult(
                provider="gohighlevel",
                status=CrmSyncStatus.UPDATED_EXISTING if is_existing else CrmSyncStatus.SYNCED,
                crm_record_id=crm_id,
                is_existing_contact=is_existing,
                attempts=attempts,
            )
        except Exception as exc:
            return CrmSyncResult(
                provider="gohighlevel",
                status=CrmSyncStatus.FAILED,
                attempts=self.settings.CRM_MAX_RETRIES,
                error_message=str(exc),
            )


class SalesforceCRMClient:
    """Salesforce REST API (v60.0) Lead/Contact adapter with SOQL email lookup and retry backoff."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.CRM_API_KEY:
            raise ValueError("CRM_API_KEY is required when CRM_PROVIDER='salesforce'.")
        self.token = settings.CRM_API_KEY.get_secret_value()
        self.base_url = settings.CRM_BASE_URL.rstrip("/")

    async def sync_lead(
        self,
        enquiry_id: str,
        extracted: ExtractedEnquiryData,
        scoring: ScoreBreakdown,
        needs_review: bool,
    ) -> CrmSyncResult:
        if extracted.is_spam_or_irrelevant:
            return CrmSyncResult(
                provider="salesforce",
                status=CrmSyncStatus.SKIPPED_SPAM,
                attempts=1,
            )

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        norm_email = normalize_email(extracted.contact_email)
        name_parts = (extracted.contact_name or "Unknown Lead").strip().split(" ")
        first_name = name_parts[0] if len(name_parts) > 1 else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else name_parts[0]

        lead_payload = {
            "FirstName": first_name,
            "LastName": last_name or "Lead",
            "Company": extracted.company_name or "Individual Enquiry",
            "Email": norm_email or None,
            "Phone": extracted.contact_phone,
            "Rating": scoring.tier.value,
            "Description": (
                f"[AI Score: {scoring.total_score}/100 | Tier: {scoring.tier.value} | "
                f"Needs Review: {needs_review}]\n{extracted.ai_summary}"
            ),
        }

        async def _sf_upsert() -> tuple[str, bool]:
            timeout = httpx.Timeout(self.settings.CRM_TIMEOUT_SECONDS, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                existing_id: str | None = None
                if norm_email:
                    # Parameterized search endpoint to avoid SOQL injection
                    search_resp = await client.post(
                        f"{self.base_url}/services/data/v60.0/parameterizedSearch",
                        headers=headers,
                        json={
                            "q": norm_email,
                            "sobjects": [{"name": "Lead", "fields": ["Id", "Email"]}],
                            "in": "EMAIL",
                        },
                    )
                    if search_resp.status_code in TRANSIENT_STATUS_CODES:
                        raise TransientAPIError(
                            f"Salesforce search HTTP {search_resp.status_code}",
                            status_code=search_resp.status_code,
                        )
                    search_resp.raise_for_status()
                    records = search_resp.json().get("searchRecords", [])
                    if records:
                        existing_id = str(records[0]["Id"])

                if existing_id:
                    patch_resp = await client.patch(
                        f"{self.base_url}/services/data/v60.0/sobjects/Lead/{existing_id}",
                        headers=headers,
                        json=lead_payload,
                    )
                    if patch_resp.status_code in TRANSIENT_STATUS_CODES:
                        raise TransientAPIError(
                            f"Salesforce update HTTP {patch_resp.status_code}",
                            status_code=patch_resp.status_code,
                        )
                    patch_resp.raise_for_status()
                    return existing_id, True

                create_resp = await client.post(
                    f"{self.base_url}/services/data/v60.0/sobjects/Lead",
                    headers=headers,
                    json=lead_payload,
                )
                if create_resp.status_code in TRANSIENT_STATUS_CODES:
                    raise TransientAPIError(
                        f"Salesforce create HTTP {create_resp.status_code}",
                        status_code=create_resp.status_code,
                    )
                create_resp.raise_for_status()
                return str(create_resp.json()["id"]), False

        try:
            (crm_id, is_existing), attempts = await execute_with_retry(
                operation_name="salesforce_crm_upsert",
                func=_sf_upsert,
                max_retries=self.settings.CRM_MAX_RETRIES,
            )
            return CrmSyncResult(
                provider="salesforce",
                status=CrmSyncStatus.UPDATED_EXISTING if is_existing else CrmSyncStatus.SYNCED,
                crm_record_id=crm_id,
                is_existing_contact=is_existing,
                attempts=attempts,
            )
        except Exception as exc:
            return CrmSyncResult(
                provider="salesforce",
                status=CrmSyncStatus.FAILED,
                attempts=self.settings.CRM_MAX_RETRIES,
                error_message=str(exc),
            )


def build_crm_client(settings: Settings) -> CRMClientProtocol:
    """Factory returning the configured CRM adapter."""
    if settings.CRM_PROVIDER == "hubspot":
        return HubSpotCRMClient(settings)
    if settings.CRM_PROVIDER == "gohighlevel":
        return GoHighLevelCRMClient(settings)
    if settings.CRM_PROVIDER == "salesforce":
        return SalesforceCRMClient(settings)
    return MockCRMClient(
        db_path=settings.DATABASE_PATH,
        max_retries=settings.CRM_MAX_RETRIES,
    )

