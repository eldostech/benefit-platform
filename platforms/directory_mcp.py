"""The plan directory's MCP server: ``build_directory_server()``.

Four read tools over the directory API -- the one way an agent or the
Operation Workbench's routing learns which system hosts a customer's plans.
Like the platforms' servers it holds nothing and decides nothing: each tool
is one call to the directory API, its result the Workbench's read contract
(``{data, evidence}``), its failures ``tool-error/v1``.

No ``from __future__ import annotations``: FastMCP reads the signatures at runtime.
"""

from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from platforms.config import DIRECTORY, Directory
from platforms.mcp_server import READ, _close_schemas, _failure, _from_http, _ok, _result


def build_directory_server(directory: Directory = DIRECTORY, *, client: httpx.AsyncClient | None = None) -> FastMCP:
    systems = ", ".join(p.name for p in directory.sources)
    holder: dict[str, httpx.AsyncClient] = {}

    def http() -> httpx.AsyncClient:
        if client is not None:
            return client
        if "client" not in holder:
            holder["client"] = httpx.AsyncClient(
                base_url=directory.api_base_url, headers={"X-API-Key": directory.api_key}, timeout=httpx.Timeout(30.0, connect=5.0)
            )
        return holder["client"]

    async def call(path: str, params: dict[str, Any] | None = None) -> CallToolResult:
        try:
            resp = await http().get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return _failure(
                "upstream_unavailable",
                f"The {directory.name} API is unreachable at {directory.api_base_url}: {exc!r}",
                hint="Check that the directory API is running.",
            )
        except httpx.HTTPError as exc:
            return _failure("timeout", f"The {directory.name} API did not answer in time: {exc!r}", hint="Retry shortly.")
        if resp.status_code >= 400:
            return _from_http(directory, resp)  # type: ignore[arg-type]
        data = resp.json()
        summary = f"{directory.name} GET {path} answered {resp.status_code}" + ("" if data.get("complete", True) else " (incomplete)")
        return _ok(_result(directory, data, write=False, summary=summary))  # type: ignore[arg-type]

    mcp = FastMCP(
        name=f"{directory.code}-mcp",
        instructions=(
            f"The enterprise plan directory: which admin system ({systems}) hosts each of a customer's plans, asked live "
            "of every system. Find a customer with search_customers or get_customer, its plans with search_plans. An "
            "answer with complete=false is missing a system that did not answer; never route on it as if it were whole."
        ),
        host="127.0.0.1",
        port=directory.mcp_port,
        stateless_http=True,
        log_level="WARNING",
    )

    @mcp.tool(
        annotations=READ,
        description=(
            f"Use this to find where a customer's plans are hosted: every plan on every system ({systems}), each with the "
            "admin_system that holds it, its product line, status and dates. Do not use it to read a plan's terms or "
            "coverages, or the customer's address; the hosting platform's own get_plan and get_customer answer for those. Returns plans[], customers[] "
            "and complete, which is false when a system did not answer. Errors: validation_failed_upstream if neither a "
            "customer number nor a name is given, or the number is malformed."
        ),
    )
    async def search_plans(
        customer_number: Annotated[Optional[str], Field(min_length=8, max_length=8, description="Eight digits starting 00 or 01.")] = None,
        customer_name: Annotated[Optional[str], Field(min_length=2, max_length=80, description="Part of the legal name.")] = None,
        product_line: Annotated[Optional[Literal["STD", "LTD", "DENTAL", "VISION"]], Field(description="Only this product line.")] = None,
        status: Annotated[Optional[Literal["active", "terminated"]], Field(description="Only plans in this status.")] = None,
    ) -> CallToolResult:
        return await call(
            "/api/v1/plans",
            {"customer_number": customer_number, "customer_name": customer_name, "product_line": product_line, "status": status},
        )

    @mcp.tool(
        annotations=READ,
        description=(
            "Use this to find customers by part of their name or the start of their number, and which systems each is "
            "on. Do not use it to list plans; use search_plans. Returns customers[] with customer_number, legal_name "
            "and hosted_on, and complete. Errors: invalid_input if the text is shorter than two characters."
        ),
    )
    async def search_customers(
        q: Annotated[str, Field(min_length=2, max_length=80, description="Part of the name, or the start of the number.")],
    ) -> CallToolResult:
        return await call("/api/v1/customers", {"q": q})

    @mcp.tool(
        annotations=READ,
        description=(
            "Use this to read one customer across every system: its legal name, the systems it is on, and every plan it "
            "holds there. Do not use it to search by name; use search_customers. Returns the customer with hosted_on, "
            "plans[] and complete. Errors: not_found if no system hosts the number; upstream_unavailable if a system "
            "that might did not answer."
        ),
    )
    async def get_customer(
        customer_number: Annotated[str, Field(min_length=8, max_length=8, description="Eight digits starting 00 or 01.")],
    ) -> CallToolResult:
        return await call(f"/api/v1/customers/{quote(customer_number, safe='')}")

    @mcp.tool(
        annotations=READ,
        description=(
            "Use this to learn which system hosts one plan id. Do not use it to read the plan itself; the hosting "
            "platform's get_plan does. Returns the plan's admin_system, customer, product line and status. Errors: "
            "not_found if no system hosts it; upstream_unavailable if a system that might did not answer."
        ),
    )
    async def get_plan(
        plan_id: Annotated[str, Field(min_length=3, max_length=20, description="A plan id, e.g. PA-STD-100001.")],
    ) -> CallToolResult:
        return await call(f"/api/v1/plans/{quote(plan_id, safe='')}")

    _close_schemas(mcp, directory)  # type: ignore[arg-type]
    return mcp
