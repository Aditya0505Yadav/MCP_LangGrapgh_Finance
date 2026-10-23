"""The single retry layer: ownership, backoff, classification."""

import pytest

from app.retry import AttemptOutcome, Classification, run_with_retries


async def test_retries_transient_then_succeeds():
    calls = {"n": 0}

    async def op(attempt: int):
        calls["n"] += 1
        if attempt < 3:
            raise ValueError("transient")
        return "ok"

    out = await run_with_retries(
        op, max_attempts=3,
        classify=lambda e: Classification(True, 0.0, "transient"),
    )
    assert out == "ok"
    assert calls["n"] == 3


async def test_non_retryable_stops_immediately():
    calls = {"n": 0}

    async def op(attempt: int):
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_with_retries(
            op, max_attempts=3, classify=lambda e: Classification(False),
        )
    assert calls["n"] == 1  # no pointless retries


async def test_exhausts_max_attempts():
    calls = {"n": 0}

    async def op(attempt: int):
        calls["n"] += 1
        raise ValueError("still failing")

    with pytest.raises(ValueError):
        await run_with_retries(
            op, max_attempts=3, classify=lambda e: Classification(True, 0.0),
        )
    assert calls["n"] == 3


async def test_records_every_attempt():
    outcomes: list[AttemptOutcome] = []

    async def op(attempt: int):
        if attempt < 2:
            raise ValueError()
        return 1

    async def record(o: AttemptOutcome):
        outcomes.append(o)

    await run_with_retries(
        op, max_attempts=3, classify=lambda e: Classification(True, 0.0),
        record_attempt=record,
    )
    assert [o.attempt_number for o in outcomes] == [1, 2]
    assert outcomes[0].succeeded is False and outcomes[1].succeeded is True
