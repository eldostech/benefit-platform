"""What the Workbench is told about Platform A and Platform B: their MCP
servers, admin systems, change types and channel rules, and where their
employers and plans come from.

Routing is data in the Workbench (its `06` design), so this is the whole of
the integration: no Workbench code knows these platforms exist.

How a request uses it:

1. the orchestrator matches the sentence to a change type here by its
   ``description``, and asks for its ``required_inputs``;
2. routing asks the enterprise plan directory, live, for the customer's
   plans and the admin system hosting each (the Workbench stores none);
3. the channel rule for (system, change type) names the platform's agent
   (``platform.platform-a``) and the admin system names its MCP server
   (``platform-a-rest-mcp``); the change type's ``operations`` say which of
   that server's tools the task may call;
4. ``verify.evaluator`` reads each plan back with ``get_plan`` against the
   ``success_criteria_template``.

**The address is the customer's.** Both platforms hold one mailing address
per customer, for all its plans there, so the address change writes
``update_customer_address`` and reads ``get_customer``, and its channel rules
say ``applies_to: customer``: routing makes one target per customer per
platform, not one per plan. Harbor Point, on both platforms, is two changes.

**Platform A's address goes through its screens.** TRACE replays a
recorded browser workflow ("Update address platform A") on Platform A's
admin screen; its MCP server is registered as Platform A's ``browser``
channel (``platform-a-trace-mcp``) with only the tools that need: the
use case's own tool and the two that read a batch back. The address rule on
Platform A names that channel alone, and the change is read back from
Platform A's own API (``platform-a-rest.get_customer``), not from TRACE's
report of its replay. Platform B's address still goes over its API.

A coverage is product-specific, so adding one is three change types
(disability, dental, vision), each with the terms its products take; the
platform that does not hold a product line has an explicit unsupported rule
for it, so a dental coverage can never be routed to Platform A.
"""

from __future__ import annotations

from typing import Any

PLATFORMS: dict[str, dict[str, Any]] = {
    "platform-a": {
        "name": "Platform A",
        "description": "Disability administration (STD, LTD): the benefit-platforms REST API and its MCP server.",
        "connector_id": "platform-a-rest-mcp",
        "mcp_url": "http://127.0.0.1:8211/mcp",
        "api_url": "http://127.0.0.1:8201",
        "api_key_env": "PLATFORM_A_API_KEY",
        "product_lines": ["disability.std", "disability.ltd"],
        "trace": {
            "connector_id": "platform-a-trace-mcp",
            "mcp_url": "http://127.0.0.1:8712/mcp",
            # TRACE names a use case's tool after it and its id's first six
            # characters; a re-recorded use case is a new id, and a new name here.
            "tools": ["update_address_platform_a_e53126", "get_batch_status", "get_batch_results"],
            "change_types": {"address.change": "update_address_platform_a_e53126"},
        },
    },
    "platform-b": {
        "name": "Platform B",
        "description": "Dental and vision administration: the benefit-platforms REST API and its MCP server.",
        "connector_id": "platform-b-rest-mcp",
        "mcp_url": "http://127.0.0.1:8212/mcp",
        "api_url": "http://127.0.0.1:8202",
        "api_key_env": "PLATFORM_B_API_KEY",
        "product_lines": ["dental", "vision"],
    },
}

OWNER = "policyholder-services"
ALL_LINES = ["disability.std", "disability.ltd", "dental", "vision"]
CLASSES = ["ALL", "SALARIED", "HOURLY", "EXECUTIVE"]
TIERS = ["2_TIER", "3_TIER", "4_TIER"]


def _input(name: str, schema: dict[str, Any], ask: str) -> dict[str, Any]:
    return {"name": name, "schema": schema, "obtainable_from": None, "ask_wording": ask, "pii_class": None}


def _date(name: str, ask: str) -> dict[str, Any]:
    return _input(name, {"type": "string", "format": "date"}, ask)


def _check(statement: str, expect: dict[str, str], *, criterion: str, record: str = "plan") -> list[dict[str, Any]]:
    """One criterion, read back with the platform's ``get_plan`` (or, for the
    customer record, ``get_customer``). ``expect`` names the record's own
    fields (``address.line1``, ``coverages[]...``): the verifier reads it and
    judges them. ``{system_plan_key}`` is the plan id, or the customer number
    for a customer-record target."""
    tool, arg = ("get_customer", "customer_number") if record == "customer" else ("get_plan", "plan_id")
    return [{"id": criterion, "statement": statement, "check": {"tool": tool, "args": {arg: "{system_plan_key}"}, "expect": expect}}]


def _coverage_add(product: str, lines: list[str], codes: list[str], terms: list[dict[str, Any]], example: str) -> dict[str, Any]:
    return {
        "id": f"coverage.add.{product}",
        "name": f"Add {product} coverage",
        "description": (
            f"Add a {product} coverage to an employer's {' or '.join(lines)} plan from a date: \"{example}\". "
            f"One coverage code ({', '.join(codes)}) for one employee class (ALL, SALARIED, HOURLY or EXECUTIVE). "
            "Only the plans of the product line the code belongs to; a plan that already has that coverage active for "
            "that class refuses it."
        ),
        "product_line_ids": lines,
        "required_inputs": [
            _input("coverage_code", {"type": "string", "enum": codes}, f"Which coverage should be added ({', '.join(codes)})?"),
            _input("employee_class", {"type": "string", "enum": CLASSES}, "For which employees: ALL, SALARIED, HOURLY or EXECUTIVE?"),
            _date("effective_date", "From what date does the coverage start?"),
            *terms,
        ],
        "derived_inputs": [],
        "irreversible": False,
        "regulated": False,
        "verification_owner": OWNER,
        "success_criteria_template": _check(
            "{plan_id} has an active {coverage_code} coverage for {employee_class} employees from {effective_date}",
            {
                "coverages[].coverage_code": "{coverage_code}",
                "coverages[].employee_class": "{employee_class}",
                "coverages[].effective_date": "{effective_date}",
                "coverages[].status": "active",
            },
            criterion="coverage_added",
        ),
        "operations": {"rest": {"write": ["add_coverage"], "read": ["get_plan", "list_coverage_options"]}},
    }


#: The change types that change the customer record, once per platform,
#: rather than each plan (a channel entry's ``applies_to``).
CUSTOMER_RECORD = {"address.change"}

CHANGE_TYPES: list[dict[str, Any]] = [
    {
        "id": "address.change",
        "name": "Address change",
        "description": (
            "Change an employer's mailing address: \"Harbor Point Manufacturing moved to 1 Riverside Plaza, Columbus OH "
            "43215\". The address is the customer's, one for all its plans on a platform, so it is changed once on each "
            "platform the customer is on. The address takes effect at once; there is no effective date."
        ),
        "product_line_ids": ALL_LINES,
        "required_inputs": [
            _input("street", {"type": "string", "minLength": 3, "maxLength": 120}, "What is the new street address (for example 200 Main Street)?"),
            _input("city", {"type": "string", "minLength": 1, "maxLength": 80}, "Which city?"),
            _input("state", {"type": "string", "pattern": "^[A-Z]{2}$"}, "Which state, as its two-letter code (for example OH)?"),
            _input("postal_code", {"type": "string", "pattern": "^[0-9]{5}(-[0-9]{4})?$"}, "What is the ZIP code?"),
        ],
        "derived_inputs": [],
        "irreversible": False,
        "regulated": False,
        "verification_owner": OWNER,
        "success_criteria_template": _check(
            "Customer {system_plan_key} shows the mailing address {street}, {city}, {state} {postal_code}",
            {"address.line1": "{street}", "address.city": "{city}", "address.state": "{state}", "address.postal_code": "{postal_code}"},
            criterion="address_set",
            record="customer",
        ),
        "operations": {
            "rest": {"write": ["update_customer_address"], "read": ["get_customer"]},
            # Platform A's screens, through TRACE; read back from Platform A's API.
            "browser": {
                "write": ["update_address_platform_a_e53126"],
                "read": ["platform-a-rest.get_customer", "get_batch_status", "get_batch_results"],
            },
        },
    },
    _coverage_add(
        "disability",
        ["disability.std", "disability.ltd"],
        ["STD_CORE", "STD_BUYUP", "LTD_CORE", "LTD_BUYUP"],
        [
            _input("benefit_pct", {"type": "integer", "minimum": 40, "maximum": 80}, "What benefit percentage of earnings (40 to 80)?"),
            _input(
                "max_benefit",
                {"type": "number", "minimum": 100, "maximum": 25000},
                "What is the maximum benefit (weekly for STD, 100 to 5,000; monthly for LTD, 500 to 25,000)?",
            ),
            _input(
                "elimination_days",
                {"type": "integer", "enum": [0, 7, 14, 90, 180]},
                "What is the elimination period in days (STD: 0, 7 or 14; LTD: 90 or 180)?",
            ),
        ],
        "Add STD buy-up for Harbor Point's salaried employees from January 1, 2027 at 70%, up to $2,500 a week, 7-day elimination",
    ),
    _coverage_add(
        "dental",
        ["dental"],
        ["DENTAL_PPO_BASE", "DENTAL_PPO_PLUS"],
        [
            _input("tier_structure", {"type": "string", "enum": TIERS}, "Which tier structure: 2_TIER, 3_TIER or 4_TIER?"),
            _input("annual_max", {"type": "number", "minimum": 500, "maximum": 5000}, "What is the annual maximum per member (500 to 5,000)?"),
        ],
        "Add dental PPO Plus for Meadowbrook's salaried staff from 2027-01-01, 4-tier, $2,500 annual maximum",
    ),
    _coverage_add(
        "vision",
        ["vision"],
        ["VISION_BASE", "VISION_PLUS"],
        [
            _input("tier_structure", {"type": "string", "enum": TIERS}, "Which tier structure: 2_TIER, 3_TIER or 4_TIER?"),
            _input("frequency_months", {"type": "integer", "enum": [12, 24]}, "How often are exams and lenses covered: every 12 or 24 months?"),
        ],
        "Add vision base for Pinecrest's hourly employees from March 1, 2027, 2-tier, every 24 months",
    ),
    {
        "id": "coverage.terminate",
        "name": "Terminate a coverage",
        "description": (
            "End one coverage on an employer's plan from a date, with a reason: \"terminate Harbor Point's STD buy-up for "
            "salaried employees effective December 31, 2026 -- withdrawn at renewal\". Only the plan of the product line the "
            "coverage belongs to (its code starts with STD_, LTD_, DENTAL_ or VISION_); the plan itself stays. There is no "
            "undo: a coverage ended in error is added again."
        ),
        "product_line_ids": ALL_LINES,
        "required_inputs": [
            _input(
                "coverage_code",
                {"type": "string", "enum": ["STD_CORE", "STD_BUYUP", "LTD_CORE", "LTD_BUYUP", "DENTAL_PPO_BASE", "DENTAL_PPO_PLUS", "VISION_BASE", "VISION_PLUS"]},
                "Which coverage should end?",
            ),
            _input("employee_class", {"type": "string", "enum": CLASSES}, "For which employees: ALL, SALARIED, HOURLY or EXECUTIVE?"),
            _date("termination_date", "On what date does the coverage end?"),
            _input("reason", {"type": "string", "minLength": 3, "maxLength": 200}, "Why is it ending?"),
        ],
        "derived_inputs": [],
        "irreversible": True,
        "regulated": False,
        "verification_owner": OWNER,
        "success_criteria_template": _check(
            "{plan_id}'s {coverage_code} coverage for {employee_class} employees is terminated effective {termination_date}",
            {
                "coverages[].coverage_code": "{coverage_code}",
                "coverages[].employee_class": "{employee_class}",
                "coverages[].status": "terminated",
                "coverages[].termination_date": "{termination_date}",
            },
            criterion="coverage_terminated",
        ),
        "operations": {"rest": {"write": ["terminate_coverage"], "read": ["get_plan"]}},
    },
    {
        "id": "contribution.change",
        "name": "Contribution change",
        "description": (
            "Change who pays for an employer's plans and how much the employee pays: \"Harbor Point's dental is now 40% "
            "employee-paid, shared\", \"make Summit Valley's STD fully employee-paid\". The employee contribution as a "
            "percentage (0 to 100) and the basis: employer_paid, employee_paid or shared. Every plan of the employer unless "
            "the person names some."
        ),
        "product_line_ids": ALL_LINES,
        "required_inputs": [
            _input(
                "employee_contribution_pct",
                {"type": "number", "minimum": 0, "maximum": 100},
                "What share of the premium does the employee pay, as a percentage (0 to 100)?",
            ),
            _input(
                "contribution_basis",
                {"type": "string", "enum": ["employer_paid", "employee_paid", "shared"]},
                "Is it employer_paid, employee_paid or shared?",
            ),
        ],
        "derived_inputs": [],
        "irreversible": False,
        "regulated": False,
        "verification_owner": OWNER,
        "success_criteria_template": _check(
            "{plan_id} shows an employee contribution of {employee_contribution_pct}% on a {contribution_basis} basis",
            {"employee_contribution_pct": "{employee_contribution_pct}", "contribution_basis": "{contribution_basis}"},
            criterion="contribution_set",
        ),
        "operations": {"rest": {"write": ["update_plan"], "read": ["get_plan"]}},
    },
    {
        "id": "billing.change",
        "name": "Billing frequency change",
        "description": (
            "Change how often an employer's plans bill: \"bill Cedar Ridge quarterly\", \"move Harbor Point's vision to "
            "annual billing\". MONTHLY, QUARTERLY or ANNUAL. Every plan of the employer unless the person names some."
        ),
        "product_line_ids": ALL_LINES,
        "required_inputs": [
            _input(
                "billing_frequency", {"type": "string", "enum": ["MONTHLY", "QUARTERLY", "ANNUAL"]}, "Bill MONTHLY, QUARTERLY or ANNUAL?"
            ),
        ],
        "derived_inputs": [],
        "irreversible": False,
        "regulated": False,
        "verification_owner": OWNER,
        "success_criteria_template": _check(
            "{plan_id} bills {billing_frequency}", {"billing_frequency": "{billing_frequency}"}, criterion="billing_set"
        ),
        "operations": {"rest": {"write": ["update_plan"], "read": ["get_plan"]}},
    },
]


def admin_system(code: str, current: dict[str, Any]) -> dict[str, Any]:
    """The admin system's next version: the current one, pointed at the real
    platform's MCP server and API instead of the sandbox."""
    p = PLATFORMS[code]
    keep = {k: current[k] for k in ("owning_department", "maintenance_windows", "migration") if k in current}
    connectors = {"rest": p["connector_id"]}
    if p.get("trace"):
        connectors["browser"] = p["trace"]["connector_id"]
    return {
        **keep,
        "code": code,
        "name": p["name"],
        "description": p["description"],
        "environments": [{"name": "dev", "base_url": p["api_url"], "connector_env": "dev"}],
        "channels_available": list(connectors),
        "connector_ids": connectors,
        "health_source": p["connector_id"],
    }


def channel_rule(code: str, change_type: dict[str, Any]) -> dict[str, Any]:
    """Every (system, change type) pair has a row: supported where the
    platform holds one of the change type's product lines, and an explicit
    refusal where it holds none -- so routing itself, before any API, never
    sends dental work to Platform A."""
    p = PLATFORMS[code]
    held = [line for line in change_type["product_line_ids"] if line in p["product_lines"]]
    if not held:
        return {
            "supported": False,
            "unsupported_reason": f"{p['name']} holds {' and '.join(p['product_lines'])} plans only.",
            "channels": [],
            "exception_department": OWNER,
            "max_wait_minutes": 240,
            "notes": f"{change_type['name']} does not apply to {p['name']}'s product lines.",
        }
    trace = p.get("trace") or {}
    if change_type["id"] in trace.get("change_types", {}):
        return {
            "supported": True,
            "unsupported_reason": None,
            "channels": [
                {
                    "channel": "browser",
                    "preference": 1,
                    "capability": f"platform.{code}",
                    "connector_id": trace["connector_id"],
                    "batchable": False,
                    "irreversible": True,
                    "requires_confirmation": True,
                    "evidence_kind": "api_response",
                    # A screen takes no idempotency key: read before writing.
                    "read_before_write": True,
                    "applies_to": "customer" if change_type["id"] in CUSTOMER_RECORD else "plan",
                }
            ],
            "exception_department": OWNER,
            "max_wait_minutes": 240,
            "notes": (
                f"{p['name']}'s admin screen, through TRACE's recorded workflow ({trace['connector_id']}); read back "
                f"from {p['name']}'s API. A refusal goes to Policyholder Services."
            ),
        }
    return {
        "supported": True,
        "unsupported_reason": None,
        "channels": [
            {
                "channel": "rest",
                "preference": 1,
                "capability": f"platform.{code}",
                "batchable": False,
                # Neither platform has an undo tool for any of these, so every
                # change waits for a person's confirmation.
                "irreversible": True,
                "requires_confirmation": True,
                "evidence_kind": "api_response",
                "applies_to": "customer" if change_type["id"] in CUSTOMER_RECORD else "plan",
            }
        ],
        "exception_department": OWNER,
        "max_wait_minutes": 240,
        "notes": f"{p['name']}'s MCP server ({p['connector_id']}) over its REST API; a refusal goes to Policyholder Services.",
    }


def manifest(code: str, draft: dict[str, Any]) -> dict[str, Any]:
    """The connector manifest the Workbench drafted from the live server
    (`discover`), finished: a name, the owning department, and each write
    tool's read-back."""
    p = PLATFORMS[code]
    server = draft["capabilities"][0]["tool"].split(".", 1)[0]
    caps = []
    for cap in draft["capabilities"]:
        cap = dict(cap)
        if not cap.get("reversible"):  # the writes; the address is read back from the customer
            reader = "get_customer" if cap["tool"].endswith(".update_customer_address") else "get_plan"
            cap["verifies_with"] = f"{server}.{reader}"
        caps.append(cap)
    return {
        **draft,
        "id": p["connector_id"],
        "name": f"{p['name']} plan administration (MCP)",
        "owning_department": "integration-services",
        "environments": {"dev": {"endpoint": p["mcp_url"], "enabled": True}},
        "capabilities": caps,
    }


#: How long TRACE's use-case tool may take: it waits up to 240 s for its
#: batch (the worker rule), and the MCP client reads for at most 300 s.
TRACE_TOOL_TIMEOUT_S = 300


def trace_manifest(code: str, draft: dict[str, Any]) -> dict[str, Any]:
    """TRACE's MCP server as the platform's ``browser`` connector, drafted by
    the Workbench from the live server (`discover`) and cut to the tools this
    platform's rules name: TRACE lists every published use case, and only
    this platform's are its business."""
    p = PLATFORMS[code]
    trace = p["trace"]
    wanted = set(trace["tools"])
    caps = []
    for cap in draft["capabilities"]:
        if cap["name"] not in wanted:
            continue
        cap = dict(cap)
        if cap["name"] in trace["change_types"].values():
            cap["timeout_s"] = TRACE_TOOL_TIMEOUT_S
        caps.append(cap)
    missing = wanted - {c["name"] for c in caps}
    if missing:
        raise SystemExit(f"{code}: TRACE does not offer {', '.join(sorted(missing))} at {trace['mcp_url']}; is the use case published?")
    return {
        **draft,
        "id": trace["connector_id"],
        "name": f"{p['name']} screens through TRACE (MCP)",
        "owning_department": "integration-services",
        "environments": {"dev": {"endpoint": trace["mcp_url"], "enabled": True}},
        "capabilities": caps,
    }
