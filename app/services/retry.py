"""Resilient retry and exponential backoff utility (Constitution Section 2.4)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from app.security.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class TransientAPIError(Exception):
    """Raised when an external API call fails with a retryable status code or network timeout."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


async def execute_with_retry(
    operation_name: str,
    func: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.15,
    max_delay: float = 4.0,
) -> tuple[T, int]:
    """Execute an async callable with exponential backoff and jitter on transient errors.

    Returns (result, total_attempts).
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await func()
            return result, attempt
        except (TransientAPIError, httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                logger.error(
                    f"{operation_name} exhausted all {max_retries} attempts: {exc}"
                )
                break

            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None and retry_after > 0:
                delay = min(retry_after, max_delay)
            else:
                backoff = base_delay * (2 ** (attempt - 1))
                jitter = random.uniform(0.0, backoff * 0.25)
                delay = min(backoff + jitter, max_delay)

            logger.warning(
                f"{operation_name} transient failure on attempt {attempt}/{max_retries} ({exc}). "
                f"Retrying in {delay:.2f}s..."
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc

