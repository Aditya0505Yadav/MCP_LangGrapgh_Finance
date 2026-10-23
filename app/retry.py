"""Single, explicit retry layer with backoff + jitter.

Retry *ownership* lives here and in the two call sites that use it (the MCP
client wrapper and the Claude agent base). The Anthropic SDK's own retries are
turned off (max_retries=0) and LangGraph node retry policies are not used, so
attempts never multiply unexpectedly. Every observable attempt is reported via
the ``record_attempt`` callback.

Only transient failures are retried. The caller supplies a ``classify`` function
that decides, per exception, whether it is retryable, an optional provider
Retry-After delay to respect, and a stable error_code for logging.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass
class Classification:
    retryable: bool
    retry_after: float | None = None
    error_code: str | None = None
    error_type: str | None = None


@dataclass
class AttemptOutcome:
    attempt_number: int
    succeeded: bool
    error: BaseException | None
    classification: Classification | None
    sleep_seconds: float | None


ClassifyFn = Callable[[BaseException], Classification]
RecordFn = Callable[[AttemptOutcome], Awaitable[None]]


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    # Exponential backoff with full jitter.
    raw = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return random.uniform(0, raw)


async def run_with_retries(
    operation: Callable[[int], Awaitable[T]],
    *,
    max_attempts: int,
    classify: ClassifyFn,
    record_attempt: RecordFn | None = None,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
) -> T:
    """Run ``operation(attempt_number)`` with retries.

    ``operation`` receives the 1-based attempt number (useful for per-attempt
    span naming). Raises the last exception if all attempts fail or the error is
    classified as non-retryable.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await operation(attempt)
        except BaseException as exc:  # noqa: BLE001 — re-raised below after classify
            last_exc = exc
            cls = classify(exc)
            is_last = attempt >= max_attempts
            sleep_for: float | None = None
            if cls.retryable and not is_last:
                sleep_for = (
                    cls.retry_after
                    if cls.retry_after is not None
                    else _backoff_delay(attempt, base_delay, max_delay)
                )
            if record_attempt is not None:
                await record_attempt(
                    AttemptOutcome(attempt, False, exc, cls, sleep_for)
                )
            if not cls.retryable or is_last:
                raise
            assert sleep_for is not None
            await asyncio.sleep(sleep_for)
        else:
            if record_attempt is not None:
                await record_attempt(AttemptOutcome(attempt, True, None, None, None))
            return result
    # Unreachable, but keeps type checkers happy.
    assert last_exc is not None
    raise last_exc
