"""Both MCP servers over the MCP protocol (an in-memory client session),
each calling its platform's real API app."""

from __future__ import annotations

import json

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from platforms.config import PLATFORM_A, PLATFORM_B
from platforms.mcp_server import build_server
from tests.conftest import mcp_session

HARBOR = "00104217"
REQUIRED = {"search_customers", "search_plans", "get_plan", "get_customer", "update_plan", "update_customer_address", "add_coverage", "terminate_coverage"}
READS = {"list_coverage_options", "search_customers", "get_customer", "search_plans", "get_plan", "list_plan_changes", "list_customer_changes"}


def _data(result) -> dict:
    return json.loads(result.content[0].text)


async def _plan(session, customer: str, line: str) -> dict:
    found = await session.call_tool("search_plans", {"customer_number": customer, "product_line": line})
    assert not found.isError, found
    (item,) = _data(found)["data"]["items"]
    plan = await session.call_tool("get_plan", {"plan_id": item["plan_id"]})
    return _data(plan)["data"]


async def test_both_servers_offer_the_same_tools(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        async with mcp_session(PLATFORM_B) as mcp_b:
            a = {t.name for t in (await mcp_a.list_tools()).tools}
            b = {t.name for t in (await mcp_b.list_tools()).tools}
            assert a == b and REQUIRED <= a


async def test_reads_are_marked_read_only_and_writes_are_not(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        for tool in (await mcp_a.list_tools()).tools:
            assert tool.annotations.readOnlyHint is (tool.name in READS), tool.name
            if tool.name not in READS:
                assert "idempotency_key" in tool.inputSchema["required"], tool.name
        terminate = next(t for t in (await mcp_a.list_tools()).tools if t.name == "terminate_coverage")
        assert terminate.annotations.destructiveHint is True


async def test_every_description_says_when_to_use_it_what_it_returns_and_its_errors(seeded):
    # The Operation Workbench refuses to register a tool whose description lacks any of these (its 05 §1.3).
    async with mcp_session(PLATFORM_A) as mcp_a:
        for tool in (await mcp_a.list_tools()).tools:
            for part in ("Use this", "Do not", "Returns", "Errors"):
                assert part in tool.description, (tool.name, part)


async def test_every_input_schema_is_closed_and_terms_name_their_fields(seeded):
    def open_objects(schema, path="$"):
        if isinstance(schema, dict):
            if (schema.get("type") == "object" or "properties" in schema) and schema.get("additionalProperties") is not False:
                yield path
            for name, sub in (schema.get("properties") or {}).items():
                yield from open_objects(sub, f"{path}.{name}")
            for key in ("anyOf", "oneOf", "allOf"):
                for i, sub in enumerate(schema.get(key) or []):
                    yield from open_objects(sub, f"{path}.{key}[{i}]")

    async with mcp_session(PLATFORM_A) as mcp_a:
        tools = (await mcp_a.list_tools()).tools
    assert [p for t in tools for p in open_objects(t.inputSchema, t.name)] == []
    terms = next(t for t in tools if t.name == "add_coverage").inputSchema["properties"]["terms"]
    assert set(terms["properties"]) == {"benefit_pct", "max_weekly_benefit", "max_monthly_benefit", "elimination_days"}
    assert terms["properties"]["elimination_days"]["enum"] == [0, 7, 14, 90, 180]


async def test_a_plan_is_found_and_read_with_its_coverages(seeded):
    async with mcp_session(PLATFORM_B) as mcp_b:
        plan = await _plan(mcp_b, HARBOR, "DENTAL")
        assert plan["plan_id"].startswith("PB-DEN-") and plan["coverages"][0]["coverage_code"] == "DENTAL_PPO_BASE"


async def test_a_customer_is_found_by_name_with_the_products_it_holds(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        found = _data(await mcp_a.call_tool("search_customers", {"query": "harbor point"}))["data"]
        (item,) = found["items"]
        assert item["customer_number"] == HARBOR and item["products"] == ["LTD", "STD"]
        assert {p["product_line"] for p in item["plans"]} == {"STD", "LTD"}
        none = _data(await mcp_a.call_tool("search_customers", {"query": "no such employer"}))["data"]
        assert none["items"] == [] and none["total"] == 0


async def test_a_customer_is_read_with_every_plan_and_its_coverages(seeded):
    async with mcp_session(PLATFORM_B) as mcp_b:
        customer = _data(await mcp_b.call_tool("get_customer", {"customer_number": HARBOR}))["data"]
        assert customer["products"] == ["DENTAL", "VISION"]
        dental = next(p for p in customer["plans"] if p["product_line"] == "DENTAL")
        assert dental["effective_date"] and dental["coverages"][0]["coverage_code"] == "DENTAL_PPO_BASE"
        assert dental["coverages"][0]["coverage_name"] and "terms" not in dental["coverages"][0]
    async with mcp_session(PLATFORM_A) as mcp_a:
        # Lakeshore's one plan here is terminated: it is listed, but no product is held.
        lakeshore = _data(await mcp_a.call_tool("get_customer", {"customer_number": "01300072"}))["data"]
        assert lakeshore["products"] == [] and [p["status"] for p in lakeshore["plans"]] == ["terminated"]


async def test_the_address_is_read_and_changed_on_the_customer_once_for_all_its_plans(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        customer = _data(await mcp_a.call_tool("get_customer", {"customer_number": HARBOR}))["data"]
        assert customer["address"]["line1"] == "200 Main Street" and len(customer["plans"]) == 2
        plan = await _plan(mcp_a, HARBOR, "STD")
        assert "address" not in plan
        args = {
            "customer_number": HARBOR,
            "line1": "1 Riverside Plaza",
            "city": "Columbus",
            "state": "OH",
            "postal_code": "43215",
            "idempotency_key": "idem_mcp_addr_1",
            "expected_version": customer["version"],
        }
        done = await mcp_a.call_tool("update_customer_address", args)
        assert not done.isError and done.structuredContent["data"]["change_id"].startswith("A-CHG-")
        assert done.structuredContent["data"]["customer"]["address"]["line1"] == "1 Riverside Plaza"
        history = _data(await mcp_a.call_tool("list_customer_changes", {"customer_number": HARBOR}))["data"]["items"]
        assert [h["change_id"] for h in history] == [done.structuredContent["data"]["change_id"]]
        replay = await mcp_a.call_tool("update_customer_address", args)
        error = _data(replay)
        assert replay.isError and error["schema"] == "tool-error/v1" and error["code"] == "idempotent_replay"
        assert error["detail"]["result"]["data"]["change_id"] == done.structuredContent["data"]["change_id"]


async def test_a_stale_version_is_a_conflict_with_a_hint_to_read_again(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        plan = await _plan(mcp_a, HARBOR, "LTD")
        r = await mcp_a.call_tool(
            "update_plan",
            {"plan_id": plan["plan_id"], "plan_name": "Renamed", "idempotency_key": "idem_mcp_plan_1", "expected_version": plan["version"] + 3},
        )
        error = _data(r)
        assert r.isError and error["code"] == "conflict" and error["upstream_excerpt"] == "412 VERSION_MISMATCH"
        assert "get_plan" in error["recovery_hint"]


async def test_platform_bs_po_box_refusal_is_a_validation_failed_upstream_tool_error(seeded):
    async with mcp_session(PLATFORM_B) as mcp_b:
        r = await mcp_b.call_tool(
            "update_customer_address",
            {"customer_number": HARBOR, "line1": "PO Box 77", "city": "Columbus", "state": "OH", "postal_code": "43216", "idempotency_key": "idem_mcp_pobox"},
        )
        error = _data(r)
        assert r.isError and error["code"] == "validation_failed_upstream"
        assert error["upstream_excerpt"] == "422 PO_BOX_NOT_ACCEPTED" and error["fields"][0]["field"] == "line1"
        assert error["upstream_ref"]


async def test_a_coverage_from_the_other_platform_is_refused_through_mcp_too(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        plan = await _plan(mcp_a, HARBOR, "STD")
        r = await mcp_a.call_tool(
            "add_coverage",
            {
                "plan_id": plan["plan_id"],
                "coverage_code": "VISION_BASE",
                "effective_date": "2027-01-01",
                "terms": {"tier_structure": "3_TIER", "frequency_months": 12},
                "idempotency_key": "idem_mcp_cross",
            },
        )
        error = _data(r)
        assert r.isError and error["code"] == "validation_failed_upstream" and error["upstream_excerpt"] == "422 PRODUCT_NOT_OFFERED"


async def test_a_coverage_is_added_and_then_terminated(seeded):
    async with mcp_session(PLATFORM_B) as mcp_b:
        plan = await _plan(mcp_b, "00030561", "VISION")  # Pinecrest: VISION_PLUS only
        added = await mcp_b.call_tool(
            "add_coverage",
            {
                "plan_id": plan["plan_id"],
                "coverage_code": "VISION_BASE",
                "employee_class": "HOURLY",
                "effective_date": "2026-01-01",
                "terms": {"tier_structure": "2_TIER", "frequency_months": 24},
                "idempotency_key": "idem_mcp_add_1",
            },
        )
        assert not added.isError, _data(added)
        coverage_id = added.structuredContent["data"]["coverage"]["coverage_id"]
        ended = await mcp_b.call_tool(
            "terminate_coverage",
            {
                "plan_id": plan["plan_id"],
                "coverage_id": coverage_id,
                "termination_date": "2026-06-30",
                "reason": "Hourly vision folded into the plus plan",
                "idempotency_key": "idem_mcp_end_1",
            },
        )
        assert not ended.isError and ended.structuredContent["data"]["coverage"]["status"] == "terminated"
        history = await mcp_b.call_tool("list_plan_changes", {"plan_id": plan["plan_id"]})
        assert [h["operation"] for h in _data(history)["data"]["items"]] == ["terminate_coverage", "add_coverage"]


async def test_an_unknown_plan_is_not_found_with_a_hint(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        r = await mcp_a.call_tool("get_plan", {"plan_id": "PA-STD-999999"})
        error = _data(r)
        assert r.isError and error["code"] == "not_found" and "search_plans" in error["recovery_hint"]


async def test_a_platform_api_that_is_down_is_upstream_unavailable_and_retryable(seeded):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1", timeout=2) as dead:
        async with create_connected_server_and_client_session(build_server(PLATFORM_A, client=dead)) as session:
            r = await session.call_tool("get_plan", {"plan_id": "PA-STD-100001"})
    error = _data(r)
    assert r.isError and error["code"] == "upstream_unavailable" and error["retryable"] is True


async def test_a_write_returns_the_workbench_result_contract_with_its_side_effect(seeded):
    """The Operation Workbench counts a write only by the side effect its
    result declares; without one, it fails the task as having done nothing."""
    async with mcp_session(PLATFORM_B) as mcp_b:
        plan = await _plan(mcp_b, HARBOR, "DENTAL")
        args = {"customer_number": HARBOR, "line1": "1 Riverside Plaza", "city": "Columbus", "state": "OH",
                "postal_code": "43215", "idempotency_key": "idem_contract_1"}
        done = (await mcp_b.call_tool("update_customer_address", args)).structuredContent
        again = (await mcp_b.call_tool("update_customer_address", {**args, "idempotency_key": "idem_contract_2"})).structuredContent
        read = (await mcp_b.call_tool("get_plan", {"plan_id": plan["plan_id"]})).structuredContent
    (effect,) = done["side_effects"]
    assert effect == {
        "system": "platform-b",
        "channel": "rest",
        "operation": "update_customer_address",
        "external_id": done["data"]["change_id"],
        "reversible": False,
        "compensation": None,
    }
    assert done["evidence"][0]["kind"] == "api_response" and done["evidence"][0]["data"] == done["data"]
    assert again["side_effects"] == [] and again["unchanged"] is True  # the same address: nothing to do
    assert "side_effects" not in read and read["evidence"] and read["data"]["plan_id"] == plan["plan_id"]


async def test_a_refusal_names_its_fields_as_paths(seeded):
    async with mcp_session(PLATFORM_A) as mcp_a:
        r = await mcp_a.call_tool(
            "update_customer_address",
            {"customer_number": HARBOR, "line1": "1 Main St", "city": "Columbus", "state": "ZZ", "postal_code": "43215", "idempotency_key": "idem_paths_1"},
        )
    assert r.isError and _data(r)["fields"][0]["path"] == "state"
