"""Webhook HMAC-SHA256 verification and API Key authentication (Constitution Section 3.3)."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings


def compute_webhook_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Compute hex-encoded HMAC-SHA256 signature over '{timestamp}.{raw_body}'."""
    signing_input = f"{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_hmac_signature(
    secret: str,
    signature_header: str,
    timestamp_header: str,
    raw_body: bytes,
    tolerance_seconds: int = 300,
    now_epoch: float | None = None,
) -> bool:
    """Verify HMAC-SHA256 signature and timestamp replay window using constant-time comparison."""
    try:
        ts = int(timestamp_header.strip())
    except (ValueError, TypeError):
        return False

    current_time = now_epoch if now_epoch is not None else time.time()
    if abs(current_time - ts) > tolerance_seconds:
        return False

    expected_sig = compute_webhook_signature(secret=secret, timestamp=str(ts), raw_body=raw_body)
    return hmac.compare_digest(expected_sig, signature_header.strip())


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency enforcing constant-time X-API-Key authentication."""
    expected = settings.API_KEY.get_secret_value()
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )
    return x_api_key


async def verify_webhook_request(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_webhook_signature: str | None = Header(default=None, alias="X-Webhook-Signature"),
    x_webhook_timestamp: str | None = Header(default=None, alias="X-Webhook-Timestamp"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate inbound enquiry webhooks via HMAC-SHA256 signature or API Key."""
    raw_body = await request.body()
    if len(raw_body) > settings.MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds maximum allowed size of {settings.MAX_PAYLOAD_BYTES} bytes.",
        )

    # If HMAC signature headers are provided (or strictly required), verify them first
    if x_webhook_signature or settings.REQUIRE_WEBHOOK_SIGNATURE:
        if not x_webhook_signature or not x_webhook_timestamp:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Webhook-Signature or X-Webhook-Timestamp header.",
            )
        valid_hmac = verify_hmac_signature(
            secret=settings.WEBHOOK_SECRET.get_secret_value(),
            signature_header=x_webhook_signature,
            timestamp_header=x_webhook_timestamp,
            raw_body=raw_body,
            tolerance_seconds=settings.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
        )
        if not valid_hmac:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook HMAC signature or expired timestamp.",
            )
        return

    # Otherwise authenticate via X-API-Key
    expected_key = settings.API_KEY.get_secret_value()
    if not x_api_key or not hmac.compare_digest(x_api_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized webhook request: provide valid X-API-Key or X-Webhook-Signature.",
        )

