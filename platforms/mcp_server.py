"""The MCP server for one platform: ``build_server(PLATFORM_A)``.

It holds no data and no rules of its own. Every tool is one call to the
platform's REST API with the platform's key, so what an agent can do through
MCP and what a program can do through the API cannot drift apart; the API
is the system, this is a way in.

Results follow the Operation Workbench's result contract, as both text and
``structuredContent``: a read is ``{data, evidence}`` and a write ``{data,
side_effects, evidence}``, ``data`` being the API's JSON (`_result`). A failure
is ``isError`` with exactly a ``tool-error/v1`` JSON body -- the contract the
Operation Workbench reads -- whose ``code`` comes from the closed catalogue
(``not_found``, ``conflict``, ``validation_failed_upstream``, ...),
``upstream_excerpt`` keeps the API's own status and code (``422
PO_BOX_NOT_ACCEPTED``) and ``upstream_ref`` its request id. A write the API
recognises as a repeat of an idempotency key comes back as the
``idempotent_replay`` error with the first result in ``detail.result``: a
replay must never pass for a fresh write.

No ``from __future__ import annotations`` here: FastMCP reads the tools'
signatures at runtime, and they name per-platform values.
"""

import json
import logging
from datetime import date
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from platforms.config import Platform

log = logging.getLogger("platforms.mcp")

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
END = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

#: What to do next, by the API's own code. Shown to the agent as ``recovery_hint``.
HINTS = {
    "PLAN_NOT_FOUND": "Find the plan id with search_plans (by customer number or name).",
    "CUSTOMER_NOT_FOUND": "This customer has no business on this platform; the other platform may hold its plans. "
    "search_customers finds a customer by name.",
    "COVERAGE_NOT_FOUND": "get_plan lists the plan's coverages and their ids.",
    "PRODUCT_NOT_OFFERED": "list_coverage_options shows the product lines and coverage codes this platform offers.",
    "INVALID_TERMS": "list_coverage_options shows the terms each coverage code needs and their allowed values.",
    "INVALID_CUSTOMER_NUMBER": "A customer number is eight digits starting 00 or 01, e.g. 00104217.",
    "PO_BOX_NOT_ACCEPTED": "Ask for a street address; this platform cannot deliver plan documents to a PO Box.",
    "INVALID_STATE": "Use the two-letter US state code, e.g. OH.",
    "INVALID_ZIP": "Use five digits, or ZIP+4 such as 43215-1234.",
    "DUPLICATE_COVERAGE": "The plan already has this coverage for this class; terminate the existing one first to replace it.",
    "COVERAGE_ALREADY_TERMINATED": "Nothing to do: the coverage is already terminated.",
    "PLAN_TERMINATED": "A terminated plan takes no changes.",
    "CUSTOMER_INACTIVE": "An inactive customer takes no changes.",
    "VERSION_MISMATCH": "The record changed since it was read; read it again (get_plan, or get_customer for the address) and retry with its version.",
    "IDEMPOTENCY_KEY_REUSED": "Use a new idempotency key for a different change.",
    "INVALID_DATE": "Check the date against the plan's or coverage's effective date.",
    "NOTHING_TO_UPDATE": "Pass at least one field to change.",
    "CUSTOMER_REQUIRED": "Pass the customer's number, or part of its name.",
    "DIRECTORY_INCOMPLETE": "A platform did not answer the directory; do not route on a partial answer, retry shortly.",
}

PlanId = Annotated[str, Field(min_length=3, max_length=20, description="The plan id, e.g. PA-STD-100001 or PB-DEN-100001.")]
IdemKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=400,
        pattern=r"^\S+$",
        description="One key per intended change; sending the same key again never applies the change twice.",
    ),
]
Version = Annotated[
    Optional[int], Field(ge=1, le=100_000_000, description="The plan version you read (get_plan's version); a stale one is refused.")
]
CustomerNumber = Annotated[str, Field(min_length=8, max_length=8, description="Eight digits starting 00 or 01, e.g. 00104217.")]
CustomerVersion = Annotated[
    Optional[int],
    Field(ge=1, le=100_000_000, description="The customer version you read (get_customer's version); a stale one is refused."),
]
Day = Annotated[date, Field(description="A date, YYYY-MM-DD.")]


def _result(platform: Platform, data: dict[str, Any], *, write: bool, summary: str) -> dict[str, Any]:
    """The Workbench's result contract (its ``workbench/connectors/common.py``):
    every read ``{data, evidence}``, every write ``{data, side_effects,
    evidence}``. The side effect is written here, by code, from the API's own
    answer -- its ``change_id`` is the ``external_id`` -- never by the model;
    a write that changed nothing says ``unchanged`` and carries none, so a
    worker can tell "nothing to do" from "did nothing"."""
    evidence = [{"kind": "api_response", "summary": summary[:1000], "data": data}]
    if not write:
        return {"data": data, "evidence": evidence}
    change_id = data.get("change_id")
    if not change_id:
        return {"data": data, "side_effects": [], "unchanged": True, "evidence": evidence}
    effect = {
        "system": platform.code,
        "channel": "rest",
        "operation": str(data.get("operation") or "write"),
        "external_id": str(change_id),
        # Neither platform has an undo for any write: a change is reversed by
        # another change, which a person asks for.
        "reversible": False,
        "compensation": None,
    }
    return {"data": data, "side_effects": [effect], "evidence": evidence}


def _ok(result: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))], structuredContent=result)


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
    retry_after_s: int | None = None,
    hint: str = "",
    upstream_ref: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    excerpt: str | None = None,
    detail: dict[str, Any] | None = None,
) -> CallToolResult:
    body: dict[str, Any] = {
        "schema": "tool-error/v1",
        "code": code,
        "message": message[:300],
        "retryable": retryable if retryable is not None else code in ("rate_limited", "upstream_unavailable", "timeout"),
        "retry_after_s": retry_after_s,
        "recovery_hint": hint[:300],
        "upstream_ref": upstream_ref,
        "fields": fields or [],
        "upstream_excerpt": (excerpt or "")[:500] or None,
    }
    if detail:
        body["detail"] = detail
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))], isError=True)


def _from_http(platform: Platform, resp: httpx.Response) -> CallToolResult:
    """An API refusal as a ``tool-error/v1``."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    status = resp.status_code
    upstream = str(body.get("code") or "")
    message = str(body.get("message") or resp.text[:200] or f"{platform.name} API answered {status}")
    common = {
        "upstream_ref": resp.headers.get("X-Request-ID") or body.get("correlation_id"),
        # The Workbench reads a field's name as ``path``; the API says ``field``.
        "fields": [{**f, "path": f.get("field")} for f in body.get("fields") or [] if isinstance(f, dict)],
        "excerpt": f"{status} {upstream}".strip(),
        "hint": HINTS.get(upstream, ""),
    }
    if status == 400 or (status == 422 and upstream in ("VALIDATION_FAILED", "INVALID_IF_MATCH")):
        return _failure("invalid_input", message, **common)
    if status == 422:
        return _failure("validation_failed_upstream", message, **common)
    if status == 404:
        return _failure("not_found", message, **common)
    if status in (409, 412):
        return _failure("conflict", message, **common)
    if status in (401, 403):
        # Never retried by itself: a refused key resubmitted is noise at best.
        return _failure("not_permitted", message, retryable=False, **common)
    if status == 429:
        after = resp.headers.get("Retry-After")
        return _failure("rate_limited", message, retry_after_s=int(after) if after and after.isdigit() else 5, **common)
    return _failure("upstream_unavailable", message, **common)


def build_server(platform: Platform, *, client: httpx.AsyncClient | None = None) -> FastMCP:
    """The platform's MCP server. ``client`` is for tests (an ASGI transport)."""
    lines = " and ".join(platform.product_lines)
    Line = Literal[tuple(platform.product_lines)]  # type: ignore[valid-type]
    codes = {line: [o.code for o in options] for line, options in platform.coverage_catalog.items()}
    code_list = "; ".join(f"{line}: {', '.join(c)}" for line, c in codes.items())
    holder: dict[str, httpx.AsyncClient] = {}

    def http() -> httpx.AsyncClient:
        if client is not None:
            return client
        if "client" not in holder:
            holder["client"] = httpx.AsyncClient(
                base_url=platform.api_base_url, headers={"X-API-Key": platform.api_key}, timeout=httpx.Timeout(30.0, connect=5.0)
            )
        return holder["client"]

    async def call(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> CallToolResult:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if expected_version is not None:
            headers["If-Match"] = f'"{expected_version}"'
        try:
            resp = await http().request(
                method, path, params={k: v for k, v in (params or {}).items() if v is not None}, json=body, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached the API, so nothing was applied: safe to retry.
            return _failure(
                "upstream_unavailable",
                f"{platform.name} API is unreachable at {platform.api_base_url}: {exc!r}",
                hint="Check that the platform API is running.",
            )
        except httpx.TimeoutException as exc:
            # Sent, but no answer: a write may or may not have landed. The
            # idempotency key makes the retry safe; a read says which.
            return _failure(
                "timeout",
                f"{platform.name} API did not answer in time: {exc!r}",
                hint="Retry with the same idempotency key, or read the record back to see whether the change landed.",
            )
        except httpx.HTTPError as exc:
            return _failure(
                "upstream_unavailable",
                f"{platform.name} API is unreachable at {platform.api_base_url}: {exc!r}",
                hint="Check that the platform API is running.",
            )
        if resp.status_code >= 400:
            return _from_http(platform, resp)
        data = resp.json()
        write = idempotency_key is not None
        summary = f"{platform.name} API {method} {path} answered {resp.status_code}" + (
            f" with change {data.get('change_id')}" if write and data.get("change_id") else ""
        )
        result = _result(platform, data, write=write, summary=summary)
        if resp.headers.get("Idempotent-Replay") == "true":
            return _failure(
                "idempotent_replay",
                f"This idempotency key was already applied ({data.get('change_id')}); nothing was changed again.",
                retryable=False,
                upstream_ref=resp.headers.get("X-Request-ID"),
                excerpt=f"{resp.status_code} Idempotent-Replay",
                detail={"result": result},
            )
        return _ok(result)

    def plan_path(plan_id: str, *rest: str) -> str:
        return "/".join(["/api/v1/plans", quote(plan_id, safe=""), *(quote(r, safe="") for r in rest)])

    def customer_path(customer_number: str, *rest: str) -> str:
        return "/".join(["/api/v1/customers", quote(customer_number, safe=""), *(quote(r, safe="") for r in rest)])

    mcp = FastMCP(
        name=f"{platform.code}-mcp",
        instructions=(
            f"{platform.name}: plan administration for {lines} plans only. Find a customer with search_customers (by "
            "name or number) and read what it holds here -- products, plans, coverages, address -- with get_customer. "
            "The mailing address is the customer's, one for all its plans here: change it with update_customer_address. "
            "Find a plan "
            "with search_plans, read it with get_plan, then change it with update_plan, add_coverage or "
            "terminate_coverage. Every write takes an idempotency_key; pass the version you read (the plan's, or the "
            "customer's for the address) as expected_version to refuse a stale write."
        ),
        host="127.0.0.1",
        port=platform.mcp_port,
        stateless_http=True,
        log_level="WARNING",
    )

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to learn which coverages {platform.name} offers for {lines} plans and which terms each needs, "
            "before add_coverage. Do not use it to read a plan's coverages; use get_plan. Returns each product line's "
            "coverage codes with their required terms and allowed values. Errors: upstream_unavailable if the API is down."
        ),
    )
    async def list_coverage_options(
        product_line: Annotated[Optional[Line], Field(description=f"Only this line: {', '.join(platform.product_lines)}.")] = None,
    ) -> CallToolResult:
        return await call("GET", "/api/v1/coverage-options", params={"product_line": product_line})

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to find customers on {platform.name} by part of the legal name or the start of the customer "
            "number, when you do not have the exact number. Do not use it to read one customer in full; use get_customer. "
            f"Returns each match's number, name, status, mailing address, the products it holds here ({lines}) and its "
            "plan ids, a page at a time; no match means the customer has no business on this platform. Errors: "
            "upstream_unavailable if the API is down."
        ),
    )
    async def search_customers(
        query: Annotated[str, Field(min_length=2, max_length=80, description="Part of the legal name, or the start of the number.")],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
    ) -> CallToolResult:
        return await call("GET", "/api/v1/customers", params={"q": query, "limit": limit, "offset": offset})

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to read everything one customer holds on {platform.name}, to answer what a customer has, and "
            "before and after an address change. Do not use it to search by name; use search_customers. Returns its "
            "legal name, status, mailing address (the one address for all its plans here) and version, the products it "
            "has here, and each plan with its dates and coverages; a plan's full terms are get_plan's. Errors: not_found "
            "means the customer has no business on this platform (the other platform may hold its plans); "
            "validation_failed_upstream means the number is not eight digits starting 00 or 01."
        ),
    )
    async def get_customer(customer_number: CustomerNumber) -> CallToolResult:
        return await call("GET", customer_path(customer_number))

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to read the change history of a customer's record on {platform.name} (its address changes), "
            "newest first. Do not use it for a plan's changes; use list_plan_changes. Returns each change's id, operation, "
            "the fields it changed and their previous values. Errors: not_found means the customer has no business on "
            "this platform."
        ),
    )
    async def list_customer_changes(customer_number: CustomerNumber, limit: Annotated[int, Field(ge=1, le=200)] = 50) -> CallToolResult:
        return await call("GET", customer_path(customer_number, "changes"), params={"limit": limit})

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to find {platform.name} plans by customer number, customer name, product line ({lines}) or "
            "status. Do not use it to read one plan in full; use get_plan. Returns matching plans with their ids, "
            "customers, versions and active coverage counts, a page at a time. Errors: validation_failed_upstream if the "
            f"product line is not {lines} or the customer number is malformed."
        ),
    )
    async def search_plans(
        customer_number: Annotated[Optional[str], Field(min_length=8, max_length=8, description="Eight digits starting 00 or 01.")] = None,
        customer_name: Annotated[Optional[str], Field(max_length=80, description="Part of the legal name.")] = None,
        product_line: Annotated[Optional[Line], Field(description=f"One of {', '.join(platform.product_lines)}.")] = None,
        status: Annotated[Optional[Literal["active", "terminated"]], Field(description="active or terminated.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
    ) -> CallToolResult:
        return await call(
            "GET",
            "/api/v1/plans",
            params={
                "customer_number": customer_number,
                "customer_name": customer_name,
                "product_line": product_line,
                "status": status,
                "limit": limit,
                "offset": offset,
            },
        )

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to read one {platform.name} plan in full -- customer, product line, terms, every coverage and its "
            "version -- before and after a change. Do not use it to find a plan; use search_plans, nor for the mailing "
            "address, which is the customer's; use get_customer. "
            "Returns the plan with its version, which a write takes as expected_version. Errors: not_found means the "
            "plan id is not on this platform."
        ),
    )
    async def get_plan(plan_id: PlanId) -> CallToolResult:
        return await call("GET", plan_path(plan_id))

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to read a {platform.name} plan's change history, newest first, to confirm what was done. Do not "
            "use it to read the plan itself; use get_plan. Returns each change's id, operation, the fields it changed "
            "and their previous values. Errors: not_found means the plan id is not on this platform."
        ),
    )
    async def list_plan_changes(plan_id: PlanId, limit: Annotated[int, Field(ge=1, le=200)] = 50) -> CallToolResult:
        return await call("GET", plan_path(plan_id, "changes"), params={"limit": limit})

    @mcp.tool(
        annotations=WRITE,
        description=(
            f"Use this to change a {platform.name} plan's name, renewal date, billing frequency (MONTHLY, QUARTERLY, "
            "ANNUAL), employee contribution percentage or contribution basis (employer_paid, employee_paid, shared). "
            "Do not use it for the address or coverages; use update_customer_address, add_coverage or terminate_coverage. "
            "Returns the change id (the confirmation number), the fields changed with their previous values, and the "
            "plan as it now stands. Errors: conflict means the plan is terminated or changed since expected_version; "
            "validation_failed_upstream means a value was refused."
        ),
    )
    async def update_plan(
        plan_id: PlanId,
        idempotency_key: IdemKey,
        plan_name: Annotated[Optional[str], Field(min_length=3, max_length=120)] = None,
        renewal_date: Annotated[Optional[date], Field(description="YYYY-MM-DD, after the plan's effective date.")] = None,
        billing_frequency: Annotated[Optional[Literal["MONTHLY", "QUARTERLY", "ANNUAL"]], Field(description="How often the plan bills.")] = None,
        employee_contribution_pct: Annotated[Optional[float], Field(ge=0, le=100)] = None,
        contribution_basis: Annotated[Optional[Literal["employer_paid", "employee_paid", "shared"]], Field(description="Who pays.")] = None,
        expected_version: Version = None,
    ) -> CallToolResult:
        body = {
            k: v
            for k, v in {
                "plan_name": plan_name,
                "renewal_date": renewal_date.isoformat() if renewal_date else None,
                "billing_frequency": billing_frequency,
                "employee_contribution_pct": employee_contribution_pct,
                "contribution_basis": contribution_basis,
            }.items()
            if v is not None
        }
        return await call("PATCH", plan_path(plan_id), body=body, idempotency_key=idempotency_key, expected_version=expected_version)

    @mcp.tool(
        annotations=WRITE,
        description=(
            f"Use this to replace a customer's mailing address on {platform.name}: the one address for every plan the "
            "customer holds here, so one call moves them all; it takes effect at once. Do not use it to read the address; "
            "use get_customer. Returns the change id (the confirmation number), the address it replaced under previous, "
            "and the customer as it now stands; the same address again changes nothing (change_id null). Errors: "
            "validation_failed_upstream means the state, ZIP"
            + (" or a PO Box" if platform.refuses_po_box else "")
            + " was refused; conflict means the customer is inactive or changed since expected_version; not_found means "
            "the customer has no business on this platform."
        ),
    )
    async def update_customer_address(
        customer_number: CustomerNumber,
        line1: Annotated[str, Field(min_length=3, max_length=120, description="Street line, e.g. 200 Main Street.")],
        city: Annotated[str, Field(min_length=1, max_length=80)],
        state: Annotated[str, Field(min_length=2, max_length=2, description="Two-letter US state code, e.g. OH.")],
        postal_code: Annotated[str, Field(min_length=5, max_length=10, description="Five digits, or ZIP+4.")],
        idempotency_key: IdemKey,
        line2: Annotated[Optional[str], Field(max_length=120, description="Suite, floor or unit.")] = None,
        expected_version: CustomerVersion = None,
    ) -> CallToolResult:
        body = {"line1": line1, "line2": line2, "city": city, "state": state, "postal_code": postal_code}
        return await call(
            "PUT", customer_path(customer_number, "address"), body=body, idempotency_key=idempotency_key, expected_version=expected_version
        )

    @mcp.tool(
        annotations=WRITE,
        description=(
            f"Use this to add a coverage to a {platform.name} plan from a date. Do not use it to change an existing "
            "coverage; terminate it and add the new one. Codes by product line -- " + code_list + "; a code must belong "
            "to the plan's own product line, and list_coverage_options gives the terms each needs. Returns the change "
            "id and the new coverage with its coverage_id. Errors: validation_failed_upstream means the code is not "
            "offered for this plan or the terms do not fit; conflict means the same coverage is already active for "
            "that employee class, or the plan is terminated."
        ),
    )
    async def add_coverage(
        plan_id: PlanId,
        coverage_code: Annotated[str, Field(min_length=3, max_length=24, description="A code for the plan's product line.")],
        effective_date: Day,
        terms: Annotated[dict[str, Any], Field(description="The terms the code needs, e.g. {\"benefit_pct\": 60, ...}.")],
        idempotency_key: IdemKey,
        employee_class: Annotated[Literal["ALL", "SALARIED", "HOURLY", "EXECUTIVE"], Field(description="Which employees it covers.")] = "ALL",
        expected_version: Version = None,
    ) -> CallToolResult:
        body = {"coverage_code": coverage_code, "employee_class": employee_class, "effective_date": effective_date.isoformat(), "terms": terms}
        return await call(
            "POST", plan_path(plan_id, "coverages"), body=body, idempotency_key=idempotency_key, expected_version=expected_version
        )

    @mcp.tool(
        annotations=END,
        description=(
            f"Use this to terminate one coverage on a {platform.name} plan from a date, with a reason. Do not use it to "
            "end the whole plan, which this API does not do. Returns the change id and the coverage as it now stands, "
            "with its state before under previous; there is no undo, a new coverage is added instead. Errors: not_found "
            "means the coverage id is not on this plan; conflict means it is already terminated or the plan is; "
            "validation_failed_upstream means the date is before the coverage started."
        ),
    )
    async def terminate_coverage(
        plan_id: PlanId,
        coverage_id: Annotated[str, Field(min_length=3, max_length=20, description="From get_plan's coverages, e.g. A-COV-000001 or B-COV-000001.")],
        termination_date: Day,
        reason: Annotated[str, Field(min_length=3, max_length=200)],
        idempotency_key: IdemKey,
        expected_version: Version = None,
    ) -> CallToolResult:
        body = {"termination_date": termination_date.isoformat(), "reason": reason}
        return await call(
            "POST",
            plan_path(plan_id, "coverages", coverage_id, "terminate"),
            body=body,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )

    _close_schemas(mcp, platform)
    return mcp


def _terms_schema(platform: Platform) -> dict[str, Any]:
    """Every term any of the platform's coverage codes takes, as one closed
    object. Which terms a given code needs, and the exact values, the API
    decides (``INVALID_TERMS``); the schema says what may be sent at all."""
    props: dict[str, dict[str, Any]] = {}
    for options in platform.coverage_catalog.values():
        for option in options:
            for name, (kind, allowed) in option.terms.items():
                if kind == "choice":
                    seen = props.setdefault(name, {"enum": []})["enum"]
                    seen.extend(v for v in allowed if v not in seen)
                else:
                    typ = "integer" if kind == "int_range" else "number"
                    cur = props.setdefault(name, {"type": typ, "minimum": allowed[0], "maximum": allowed[1]})
                    cur["minimum"], cur["maximum"] = min(cur["minimum"], allowed[0]), max(cur["maximum"], allowed[1])
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "description": "The coverage's terms; list_coverage_options says which each code needs and their allowed values.",
    }


def _close_schemas(mcp: FastMCP, platform: Platform) -> None:
    """Close every object in every tool's input schema (``additionalProperties:
    false``). FastMCP leaves them open; a client that holds tools to closed
    schemas (the Operation Workbench refuses an open one) must see exactly the
    arguments a tool takes, and a misspelled one is then refused, not dropped."""

    def close(schema: Any) -> None:
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "object" or "properties" in schema:
            schema.setdefault("additionalProperties", False)
        for sub in (schema.get("properties") or {}).values():
            close(sub)
        close(schema.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(key) or []:
                close(sub)
        for sub in (schema.get("$defs") or {}).values():
            close(sub)

    for tool in mcp._tool_manager.list_tools():
        if "terms" in (tool.parameters.get("properties") or {}):
            tool.parameters["properties"]["terms"] = _terms_schema(platform)
        close(tool.parameters)
