"""Human-in-the-loop Outbound Email Dispatcher (Local Outbox, SMTP, and Resend API)."""

from __future__ import annotations

import smtplib
import sqlite3
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.config import Settings
from app.security.logging import get_logger
from app.services.retry import TRANSIENT_STATUS_CODES, TransientAPIError, execute_with_retry

logger = get_logger(__name__)


class EmailSenderProtocol(Protocol):
    """Interface for dispatching approved acknowledgement emails."""

    async def send_approved_email(
        self,
        enquiry_id: str,
        to_email: str,
        subject: str,
        body: str,
        approved_by: str,
    ) -> str:
        """Send an approved acknowledgement email and return a message/dispatch ID."""
        ...


class LocalOutboxEmailSender:
    """Stores approved outbound emails in SQLite `sent_emails` audit table for local verification."""

    def __init__(self, db_path: str = "data/lead_qualifier.db", from_address: str = "advisory@intelligentstack.ai") -> None:
        self.db_path = db_path
        self.from_address = from_address

    async def send_approved_email(
        self,
        enquiry_id: str,
        to_email: str,
        subject: str,
        body: str,
        approved_by: str,
    ) -> str:
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_emails (
                    message_id TEXT PRIMARY KEY,
                    enquiry_id TEXT NOT NULL,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sent_emails (
                    message_id, enquiry_id, from_address, to_address,
                    subject, body, approved_by, provider, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'local_outbox', ?)
                """,
                (msg_id, enquiry_id, self.from_address, to_email, subject, body, approved_by, now),
            )
        logger.info(
            f"Dispatched approved acknowledgement email {msg_id} for {enquiry_id} to {to_email} (approved_by={approved_by})."
        )
        return msg_id


class ResendEmailSender:
    """Production transactional email adapter using Resend REST API (https://api.resend.com/emails)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.RESEND_API_KEY:
            raise ValueError("RESEND_API_KEY is required when EMAIL_PROVIDER='resend'.")
        self.api_key = settings.RESEND_API_KEY.get_secret_value()
        self.from_address = settings.EMAIL_FROM_ADDRESS

    async def send_approved_email(
        self,
        enquiry_id: str,
        to_email: str,
        subject: str,
        body: str,
        approved_by: str,
    ) -> str:
        async def _post_resend() -> str:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"ack-{enquiry_id}",
                    },
                    json={
                        "from": self.from_address,
                        "to": [to_email],
                        "subject": subject,
                        "text": body,
                    },
                )
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    raise TransientAPIError(f"Resend HTTP {resp.status_code}", status_code=resp.status_code)
                resp.raise_for_status()
                return str(resp.json().get("id", f"resend_{enquiry_id}"))

        msg_id, _ = await execute_with_retry("resend_send_email", _post_resend, max_retries=3)
        logger.info(f"Sent Resend email {msg_id} for {enquiry_id} to {to_email} (approved_by={approved_by}).")
        return msg_id


class SMTPEmailSender:
    """Standard TLS SMTP adapter for Google Workspace, Office365, or Amazon SES."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_approved_email(
        self,
        enquiry_id: str,
        to_email: str,
        subject: str,
        body: str,
        approved_by: str,
    ) -> str:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.settings.EMAIL_FROM_ADDRESS
        msg["To"] = to_email
        msg.set_content(body)
        msg_id = f"smtp_{uuid.uuid4().hex[:12]}"

        with smtplib.SMTP(self.settings.SMTP_HOST, self.settings.SMTP_PORT, timeout=10) as server:
            if self.settings.SMTP_USE_TLS:
                server.starttls()
            if self.settings.SMTP_USERNAME and self.settings.SMTP_PASSWORD:
                server.login(
                    self.settings.SMTP_USERNAME,
                    self.settings.SMTP_PASSWORD.get_secret_value(),
                )
            server.send_message(msg)

        logger.info(f"Sent SMTP email {msg_id} for {enquiry_id} to {to_email} (approved_by={approved_by}).")
        return msg_id


def build_email_sender(settings: Settings) -> EmailSenderProtocol:
    """Factory returning configured outbound email dispatcher."""
    if settings.EMAIL_PROVIDER == "resend":
        return ResendEmailSender(settings)
    if settings.EMAIL_PROVIDER == "smtp":
        return SMTPEmailSender(settings)
    return LocalOutboxEmailSender(
        db_path=settings.DATABASE_PATH,
        from_address=settings.EMAIL_FROM_ADDRESS,
    )

