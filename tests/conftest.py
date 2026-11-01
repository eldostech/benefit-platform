"""Every test runs on its own copy of the seed, in ``platform_a_test`` and
``platform_b_test`` -- never the schemas you test the Workbench against."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

# Before anything reads a platform's schema.
os.environ["PLATFORMS_SCHEMA_SUFFIX"] = "_test"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from platforms.api import create_app  # noqa: E402
from platforms.config import PLATFORM_A, PLATFORM_B, Platform  # noqa: E402
from platforms.db import drop_schema  # noqa: E402
from platforms.mcp_server import build_server  # noqa: E402
from platforms.seed import seed  # noqa: E402

for _p in (PLATFORM_A, PLATFORM_B):
    os.environ.setdefault(_p.api_key_env, f"test-key-{_p.key}")
    assert _p.schema.endswith("_test"), _p.schema


@pytest.fixture(scope="session", autouse=True)
def _drop_test_schemas_after():
    yield
    for p in (PLATFORM_A, PLATFORM_B):
        drop_schema(p)


@pytest.fixture
def seeded():
    for p in (PLATFORM_A, PLATFORM_B):
        seed(p, reset=True)


def _client(p: Platform) -> TestClient:
    return TestClient(create_app(p), headers={"X-API-Key": p.api_key})


@pytest.fixture
def api_a(seeded):
    with _client(PLATFORM_A) as c:
        yield c


@pytest.fixture
def api_b(seeded):
    with _client(PLATFORM_B) as c:
        yield c


@asynccontextmanager
async def mcp_session(p: Platform):
    """An MCP client session on the platform's server, which calls the real API
    app in process. Opened inside each test, not in a fixture: the session's
    task group must be entered and left by one task, and pytest-asyncio sets a
    fixture up and tears it down in different ones."""
    app = create_app(p)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform.test", headers={"X-API-Key": p.api_key}
    ) as http:
        async with create_connected_server_and_client_session(build_server(p, client=http)) as session:
            yield session


def plan_id(client: TestClient, customer: str, line: str) -> str:
    items = client.get("/api/v1/plans", params={"customer_number": customer, "product_line": line}).json()["items"]
    assert len(items) == 1, items
    return items[0]["plan_id"]
