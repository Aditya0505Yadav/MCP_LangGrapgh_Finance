"""Shared test fixtures and lightweight fakes.

Unit tests here need no PostgreSQL, no MCP server and no Anthropic API. Fakes
stand in for the trace recorder and repositories so node logic can be tested in
isolation. Tests that require live services are marked ``integration``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test-key",
        data_mode="fixture",
        database_url="postgresql://finance:finance@localhost:5432/finance_demo",
    )


class FakeRecorder:
    """No-op trace recorder: yields a mutable span, swallows events."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    @asynccontextmanager
    async def span(self, **kwargs):
        sp = SimpleNamespace(
            span_id="span-fake", output_payload=None, status_override=None,
            error_code=None, provider_request_id=None, agent_role=None,
            retryable=None, **{k: v for k, v in kwargs.items()
                               if k in ("trace_id", "run_id")},
        )
        yield sp

    async def event(self, **kwargs) -> None:
        self.events.append(kwargs)


class FakeRepos:
    """No-op repositories for node-logic tests."""

    def __init__(self) -> None:
        self.reports: list[dict] = []

    async def heartbeat_attempt(self, *_a, **_k) -> None:
        return None

    async def next_report_version(self, *_a, **_k) -> int:
        return 1

    async def insert_report(self, **kwargs) -> None:
        self.reports.append(kwargs)


@pytest.fixture
def fake_recorder() -> FakeRecorder:
    return FakeRecorder()


@pytest.fixture
def fake_repos() -> FakeRepos:
    return FakeRepos()
