"""The enterprise plan directory: one API over both platforms, asked live,
and its MCP server. The platforms run in process; "down" is a platform
client pointed at a closed port."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from platforms.api import create_app
from platforms.config import DIRECTORY, PLATFORM_A, PLATFORM_B
from platforms.directory import create_directory_app
from platforms.directory_mcp import build_directory_server

HARBOR = "00104217"  # STD, LTD on A; dental, vision on B
PINECREST = "00030561"  # vision on B only


@asynccontextmanager
async def directory(*, b_down: bool = False):
    """The directory API (as an httpx client), over the real platform apps."""
    a = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(PLATFORM_A)), base_url="http://a", headers={"X-API-Key": PLATFORM_A.api_key})
    b = (
        httpx.AsyncClient(base_url="http://127.0.0.1:1", timeout=1)
        if b_down
        else httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(PLATFORM_B)), base_url="http://b", headers={"X-API-Key": PLATFORM_B.api_key})
    )
    app = create_directory_app(clients={PLATFORM_A.code: a, PLATFORM_B.code: b})
    async with a, b, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://directory", headers={"X-API-Key": DIRECTORY.api_key}
    ) as d:
        yield d


async def test_one_call_finds_a_customers_plans_on_both_systems_each_labelled_with_its_host(seeded):
    async with directory() as d:
        body = (await d.get("/api/v1/plans", params={"customer_number": HARBOR})).json()
    assert body["complete"] is True and [s["status"] for s in body["sources"]] == ["ok", "ok"]
    assert {(p["product_line"], p["admin_system"]) for p in body["plans"]} == {
        ("STD", "platform-a"),
        ("LTD", "platform-a"),
        ("DENTAL", "platform-b"),
        ("VISION", "platform-b"),
    }
    assert body["customers"] == [{"customer_number": HARBOR, "legal_name": "Harbor Point Manufacturing, Inc.", "hosted_on": ["platform-a", "platform-b"]}]


async def test_the_directory_says_where_a_plan_is_never_what_is_in_it(seeded):
    async with directory() as d:
        plans = (await d.get("/api/v1/plans", params={"customer_number": HARBOR})).json()["plans"]
    assert all(not ({"coverages", "address", "situs_state", "employee_contribution_pct", "version"} & set(p)) for p in plans)


async def test_a_customer_on_one_system_is_found_there_alone(seeded):
    async with directory() as d:
        body = (await d.get(f"/api/v1/customers/{PINECREST}")).json()
    assert body["hosted_on"] == ["platform-b"] and [p["product_line"] for p in body["plans"]] == ["VISION"]


async def test_a_product_line_one_system_does_not_hold_is_an_empty_answer_from_it_not_a_failure(seeded):
    async with directory() as d:
        body = (await d.get("/api/v1/plans", params={"customer_number": HARBOR, "product_line": "DENTAL"})).json()
    assert body["complete"] is True and [p["admin_system"] for p in body["plans"]] == ["platform-b"]


async def test_a_name_search_spans_both_systems_and_status_filters_it(seeded):
    async with directory() as d:
        every = (await d.get("/api/v1/plans", params={"customer_name": "lakeshore"})).json()["plans"]
        active = (await d.get("/api/v1/plans", params={"customer_name": "lakeshore", "status": "active"})).json()["plans"]
    assert {(p["admin_system"], p["status"]) for p in every} == {("platform-a", "terminated"), ("platform-b", "active")}
    assert {p["admin_system"] for p in active} == {"platform-b"}


async def test_customers_are_found_by_name_with_the_systems_they_are_on(seeded):
    async with directory() as d:
        body = (await d.get("/api/v1/customers", params={"q": "harbor"})).json()
    assert body["customers"][0]["hosted_on"] == ["platform-a", "platform-b"]


async def test_one_plan_id_is_located_on_its_system(seeded):
    async with directory() as d:
        a = (await d.get("/api/v1/plans/PA-STD-100001")).json()
        missing = await d.get("/api/v1/plans/PA-STD-999999")
    assert a["admin_system"] == "platform-a" and a["customer_number"] == HARBOR
    assert missing.status_code == 404 and missing.json()["code"] == "PLAN_NOT_FOUND"


async def test_unknown_malformed_and_missing_customers_are_refused(seeded):
    async with directory() as d:
        unknown = await d.get("/api/v1/customers/00999999")
        malformed = await d.get("/api/v1/plans", params={"customer_number": "02000000"})
        nobody = await d.get("/api/v1/plans")
    assert unknown.status_code == 404 and unknown.json()["code"] == "CUSTOMER_NOT_FOUND"
    assert malformed.status_code == 422 and malformed.json()["code"] == "INVALID_CUSTOMER_NUMBER"
    assert nobody.status_code == 422 and nobody.json()["code"] == "CUSTOMER_REQUIRED"


async def test_a_system_that_does_not_answer_makes_the_answer_incomplete_never_empty(seeded):
    async with directory(b_down=True) as d:
        harbor = (await d.get("/api/v1/plans", params={"customer_number": HARBOR})).json()
        pinecrest = await d.get(f"/api/v1/customers/{PINECREST}")
    assert harbor["complete"] is False
    assert {s["admin_system"]: s["status"] for s in harbor["sources"]} == {"platform-a": "ok", "platform-b": "unavailable"}
    assert {p["admin_system"] for p in harbor["plans"]} == {"platform-a"}
    # Not "no such customer": the system that may hold it did not answer.
    assert pinecrest.status_code == 503 and pinecrest.json()["code"] == "DIRECTORY_INCOMPLETE"


async def test_the_directory_needs_its_own_key(seeded):
    async with directory() as d:
        r = await d.get("/api/v1/plans", params={"customer_number": HARBOR}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


# ------------------------------------------------------------------ MCP server
@asynccontextmanager
async def directory_mcp(*, b_down: bool = False):
    async with directory(b_down=b_down) as d:
        async with create_connected_server_and_client_session(build_directory_server(client=d)) as session:
            yield session


async def test_the_directory_server_offers_four_read_only_tools_with_closed_schemas(seeded):
    async with directory_mcp() as s:
        tools = (await s.list_tools()).tools
    assert sorted(t.name for t in tools) == ["get_customer", "get_plan", "search_customers", "search_plans"]
    assert all(t.annotations.readOnlyHint for t in tools)
    assert all(t.inputSchema.get("additionalProperties") is False for t in tools)


async def test_search_plans_through_mcp_returns_the_read_contract(seeded):
    async with directory_mcp() as s:
        r = await s.call_tool("search_plans", {"customer_number": HARBOR})
    body = r.structuredContent
    assert not r.isError and set(body) == {"data", "evidence"}
    assert len(body["data"]["plans"]) == 4 and body["data"]["complete"] is True


async def test_through_mcp_a_missing_system_is_upstream_unavailable_when_it_decides_the_answer(seeded):
    async with directory_mcp(b_down=True) as s:
        partial = await s.call_tool("search_plans", {"customer_number": HARBOR})
        missing = await s.call_tool("get_customer", {"customer_number": PINECREST})
    assert not partial.isError and partial.structuredContent["data"]["complete"] is False
    error = json.loads(missing.content[0].text)
    assert missing.isError and error["code"] == "upstream_unavailable" and error["upstream_excerpt"] == "503 DIRECTORY_INCOMPLETE"
