# Benefit platforms: Platform A and Platform B

Two independent admin systems to test the Operation Workbench against, in
place of its in-process sandbox. Each has a real REST API, its own MCP
server over that API, and its own PostgreSQL schema.

| | Platform A | Platform B |
|---|---|---|
| Products | **STD, LTD** only | **Dental, Vision** only |
| Schema (fixed in `platforms/config.py`) | `platform_a` | `platform_b` |
| REST API | http://127.0.0.1:8201 (docs at `/docs`) | http://127.0.0.1:8202 (docs at `/docs`) |
| MCP server (streamable HTTP) | http://127.0.0.1:8211/mcp | http://127.0.0.1:8212/mcp |
| Admin screens (no sign-in) | http://127.0.0.1:8221 | http://127.0.0.1:8222 |
| Ids | plans `PA-STD-100001`, coverages `A-COV-000001`, changes `A-CHG-000001` | `PB-DEN-100001`, `B-COV-…`, `B-CHG-…` |
| API key | `PLATFORM_A_API_KEY` in `.env` | `PLATFORM_B_API_KEY` in `.env` |
| Extra business rule | accepts PO Boxes | **refuses a PO Box address** (`422 PO_BOX_NOT_ACCEPTED`) |

**No crisscrossing**, enforced twice: the API refuses it with
`422 PRODUCT_NOT_OFFERED`, and the database refuses it even from psql. A
plan's product line is checked against the platform's lines, and a coverage
carries its plan's line through a composite foreign key, with its code
required to start with that line. So `DENTAL_PPO_BASE` can never attach to an
STD plan, and Platform A can never hold a vision plan.

**Customer numbers** are eight digits starting `00` or `01` (`00104217`),
also checked by the API (`422 INVALID_CUSTOMER_NUMBER`) and by a CHECK
constraint. A customer that buys from both platforms exists on both under the
same number.

## Run it

```bash
.venv/Scripts/python run.py seed --reset   # (re)load the customers below; deletes every change made since
.venv/Scripts/python run.py serve          # every server (eight): APIs, MCP servers, the directory and both admin screens, until Ctrl+C
```

Each server can also run on its own: `run.py api a`, `run.py api b`,
`run.py mcp a`, `run.py mcp b`, `run.py web a`, `run.py web b`. Add `--stdio`
to `mcp` to serve over stdio instead. `run.py` works from any directory;
`python -m platforms …` does the same from this one. In the Claude desktop
app, the Workbench folder's `.claude/launch.json` has an entry for each
(`platform-a-api`, `platform-a-mcp`, `platform-a-web`, and so on).

`.env` holds the database connection (the same PostgreSQL as the Workbench)
and a generated development key for each API. It is git-ignored. To set it up
on another machine, copy `.env.example`.

```bash
.venv/Scripts/python -m pytest             # 92 tests, on platform_a_test / platform_b_test, never your data
```

## Admin screens (for browser-based testing)

Each platform also has administration screens, at http://127.0.0.1:8221
(Platform A) and http://127.0.0.1:8222 (Platform B). They are for testing a
browser-driven integration against a platform as if it took no API calls.

- **No sign-in.** Anyone who can reach the port can read and change
  anything. For local testing only; never expose them.
- **Same rules and change log as the API.** The screens call the same
  `PlatformStore`, so a change made on a screen gets a confirmation number
  from the same sequence (`A-CHG-…`). It shows in the API's history and in
  the plan directory like any other change.
- **What you can do:**
  - Search plans by customer number or name, and browse customers.
  - Open a customer to change its mailing address: one address for every
    plan it holds on that platform. A plan's page shows the address and
    links to its customer.
  - Open a plan, then:
    - change its details (name, renewal date, billing, contribution);
    - add a coverage: choose it, then fill in its terms;
    - terminate a coverage: fill in the date and reason, review, then confirm.
- **Built to be driven by a browser:**
  - Plain server-rendered forms, with no script to wait for.
  - Every control has a label, an `id` and a `data-testid` (`input-line1`,
    `save`, `confirm`). Every row has `data-testid="plan-row-<plan id>"`.
  - The confirmation page shows the change id in
    `data-testid="confirmation-number"`.
  - A refusal is shown on the page:
    - the banner has `role="alert"` and the API's own code in
      `data-error-code`, for example `PO_BOX_NOT_ACCEPTED` on Platform B;
    - the field it names is marked `aria-invalid`, with the message beside it.
- **Resubmitting is safe:**
  - Each form carries a token, sent as the write's `Idempotency-Key`, so a
    double submit is one change.
  - Each form also carries the version it read (the plan's, or the
    customer's on the address form), sent as `If-Match`. A
    form opened before someone else's change is refused with
    `VERSION_MISMATCH` instead of overwriting it.

## Enterprise plan directory (one API over both platforms)

**What it's for:** the enterprise's single answer to *"which system hosts each of this customer's plans?"*
- **API:** http://127.0.0.1:8203 (docs at `/docs`).
- **MCP server:** http://127.0.0.1:8213/mcp.
- **Key:** `PLAN_DIRECTORY_API_KEY`.

**How it answers:** it stores nothing. Every call asks Platform A and Platform B **concurrently**, through their own APIs and keys, and merges their answers. Each plan is labelled with the `admin_system` that hosts it, together with what routing needs to choose it: product line, status and dates. It never returns a plan's terms or coverages, or the customer's address; only the hosting platform answers for those.

| Method | Path | Answers |
|---|---|---|
| GET | `/api/v1/plans?customer_number=&customer_name=&product_line=&status=` | Every plan of the customer, on every system, with its host |
| GET | `/api/v1/plans/{plan_id}` | Which system hosts one plan |
| GET | `/api/v1/customers?q=` | Customers by name or number prefix, and the systems each is on |
| GET | `/api/v1/customers/{customer_number}` | One customer: its systems and every plan it holds there |

**When a platform is down:** an answer missing a system says so, with `complete: false` and that system marked `unavailable` under `sources`. A lookup that the missing system decides is `503 DIRECTORY_INCOMPLETE`, never a false "not found". A platform that doesn't hold a product line answers a search for it as empty, not as a failure.

**MCP tools** (all read-only, in the Workbench's `{data, evidence}` read contract): `search_plans`, `search_customers`, `get_customer`, `get_plan`.

## REST API (the same on both platforms)

**The mailing address is the customer's**, not a plan's. Each platform holds
one address per customer, shared by all the customer's plans there, with the
customer's own version. Moving a customer that is on both platforms is one
change on each.

Send the key as `X-API-Key: <key>` or `Authorization: Bearer <key>`.
`/health` and `/docs` are open.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/coverage-options` | The coverage codes each product line offers, and the terms each needs |
| GET | `/api/v1/customers?q=` | List or **search customers** by part of the name or the start of the number, each with the products it holds here and its plan ids |
| GET | `/api/v1/customers/{customer_number}` | One customer: its **mailing address**, the products it holds here, each plan with its dates and coverages; the version as `ETag` |
| PUT | `/api/v1/customers/{customer_number}/address` | **Update the customer's address**, for all its plans here: `line1`, `line2`, `city`, `state`, `postal_code` |
| GET | `/api/v1/customers/{customer_number}/changes` | The customer record's change history (its address changes), newest first |
| GET | `/api/v1/plans?customer_number=&customer_name=&product_line=&status=&limit=&offset=` | **Search plans** |
| GET | `/api/v1/plans/{plan_id}` | **Get a plan**: terms, coverages; the version as `ETag` |
| PATCH | `/api/v1/plans/{plan_id}` | **Update the plan**: `plan_name`, `renewal_date`, `billing_frequency`, `employee_contribution_pct`, `contribution_basis` |
| POST | `/api/v1/plans/{plan_id}/coverages` | **Add a coverage**: `coverage_code`, `employee_class`, `effective_date`, `terms` |
| POST | `/api/v1/plans/{plan_id}/coverages/{coverage_id}/terminate` | **Terminate a coverage**: `termination_date`, `reason` |
| GET | `/api/v1/plans/{plan_id}/changes` | The plan's change history, newest first |

What every write guarantees:
- **`Idempotency-Key`:** the same key and request returns the first response with `Idempotent-Replay: true`. The same key with a different request is `422 IDEMPOTENCY_KEY_REUSED`.
- **`If-Match: "<version>"`:** the version of the record written (the plan, or the customer for the address); a stale one is `412 VERSION_MISMATCH`.
- **Terminated plans and inactive customers:** a write to one is `409 PLAN_TERMINATED` or `409 CUSTOMER_INACTIVE`.
- **No-op writes:** a write that changes nothing (the same address again) returns `change_id: null` and is not recorded.
- **Change records:** every real change returns a `change_id` (the confirmation number), the changed fields with their `previous` values, and the record as it now stands (`plan`, or `customer` for the address).

Errors are `{"code", "message", "fields", "correlation_id"}`. The `code`
values are stable: `PLAN_NOT_FOUND`, `CUSTOMER_NOT_FOUND`,
`COVERAGE_NOT_FOUND`, `PRODUCT_NOT_OFFERED`, `INVALID_TERMS`,
`INVALID_STATE`, `INVALID_ZIP`, `PO_BOX_NOT_ACCEPTED`,
`DUPLICATE_COVERAGE`, `COVERAGE_ALREADY_TERMINATED`, `CUSTOMER_INACTIVE`, `INVALID_DATE`,
`NOTHING_TO_UPDATE`, `VALIDATION_FAILED`, `UNAUTHORIZED`.

### Coverage catalogue

| Line | Codes | Terms |
|---|---|---|
| STD | `STD_CORE`, `STD_BUYUP` | `benefit_pct` 40–80, `max_weekly_benefit` 100–5000, `elimination_days` 0/7/14 |
| LTD | `LTD_CORE`, `LTD_BUYUP` | `benefit_pct` 40–80, `max_monthly_benefit` 500–25000, `elimination_days` 90/180 |
| DENTAL | `DENTAL_PPO_BASE`, `DENTAL_PPO_PLUS` | `tier_structure` 2_TIER/3_TIER/4_TIER, `annual_max` 500–5000 |
| VISION | `VISION_BASE`, `VISION_PLUS` | `tier_structure` 2_TIER/3_TIER/4_TIER, `frequency_months` 12/24 |

`employee_class` is `ALL`, `SALARIED`, `HOURLY` or `EXECUTIVE`. A plan may
hold one *active* coverage per code and class.

## MCP servers (the same eleven tools on both)

`search_customers` (by name or number), `get_customer` (everything the
customer holds here: products, plans, coverages, mailing address),
`search_plans`, `get_plan`, `update_plan`, `add_coverage`,
`terminate_coverage`, `update_customer_address`, plus `list_coverage_options`, `list_plan_changes`
and `list_customer_changes`. Each tool is one call to its platform's REST
API, so MCP and REST can never disagree.

- **Annotations:** reads are marked `readOnlyHint`, and `terminate_coverage` is marked `destructiveHint`.
- **Write arguments:** every write takes `idempotency_key`, and optionally `expected_version` (sent as `If-Match`): the plan's version, or the customer's for `update_customer_address`.
- **Results:** the Operation Workbench's result contract, both as text and as `structuredContent`:
  - a read is `{data, evidence}`;
  - a write is `{data, side_effects, evidence}`, with `data` being the API's JSON;
  - each write's side effect is `{system, channel: rest, operation, external_id: <change_id>, reversible: false, compensation: null}`, built from the API's answer;
  - a write that changed nothing says `unchanged: true`;
  - the Workbench counts a write only by its side effect, and fails a task whose write declares none.
- **Failures:** `isError` with a `tool-error/v1` body, the format the Workbench reads:
  - `code` is `not_found`, `conflict`, `validation_failed_upstream`, `invalid_input`, `not_permitted`, `upstream_unavailable`, `timeout` or `idempotent_replay`;
  - `upstream_excerpt` carries the API's status and code (`422 PO_BOX_NOT_ACCEPTED`), and `upstream_ref` its request id;
  - `recovery_hint` says what to do next.
- **Repeated keys:** a repeated idempotency key comes back as `idempotent_replay`, with the first result in `detail.result`.

## Seeded data

| Customer | Name | Platform A | Platform B |
|---|---|---|---|
| 00104217 | Harbor Point Manufacturing, Inc. | PA-STD-100001 (STD_CORE all, STD_BUYUP salaried), PA-LTD-100002 | PB-DEN-100001, PB-VIS-100002 |
| 00255190 | Summit Valley School District | PA-STD-100005, PA-LTD-100006 | PB-DEN-100005, PB-VIS-100006 |
| 01022045 | Cedar Ridge Health Partners | PA-LTD-100003 (LTD_BUYUP executive) | PB-VIS-100004 |
| 01300072 | Lakeshore Hospitality Group | PA-LTD-100007 **terminated** | PB-DEN-100007 (salaried/hourly), PB-VIS-100008 |
| 01555123 | Meadowbrook Home & Garden Stores | PA-STD-100009 (hourly only) | PB-DEN-100009 |
| 01077310 | Northgate Engineering Group | PA-STD-100004 (one terminated coverage) | — |
| 00409981 | Ironwood Community Credit Union | PA-LTD-100008 | — |
| 01009874 | Granite Peak Construction | PA-STD-100010, PA-LTD-100011 (its customer record here has a PO Box address) | — |
| 00118833 | Bluestem Logistics LLC | — | PB-DEN-100003 (one terminated coverage) |
| 00030561 | Pinecrest Software Co. | — | PB-VIS-100010 |

Each customer has the same street address on both platforms, except Granite Peak, whose customer record on
Platform A still holds a legacy PO Box. The customer names are fictional.

## Wired into the Operation Workbench

The Workbench (`C:\Users\itsme\git\operation-workbench`) uses these
platforms instead of its sandbox. All of it is Workbench *data*, written by
`wiring/`; no Workbench code knows these platforms exist.

| In the Workbench | What it is now |
|---|---|
| MCP servers | `platform-a-rest-mcp` → http://127.0.0.1:8211/mcp and `platform-b-rest-mcp` → http://127.0.0.1:8212/mcp, external servers over streamable HTTP, all eleven tools each; and `platform-a-trace-mcp` → TRACE's MCP server, http://127.0.0.1:8712/mcp, with three of its tools: `update_address_platform_a_e53126` (TRACE's recorded "Update address platform A" workflow), `get_batch_status` and `get_batch_results`. Each is checked against the live listing at registration |
| Agents | `platform.platform-a` and `platform.platform-b` (generated by the Workbench): each may use only its own platform's server |
| Admin systems | `platform-a`, `platform-b`, pointed at those servers and at http://127.0.0.1:8201 / 8202. Platform A has two channels: `rest` (its API) and `browser` (its admin screen, through TRACE) |
| Change types | `address.change` (over `rest`, update_customer_address; over `browser`, TRACE's address workflow; both read back with Platform A's or B's own get_customer), `coverage.add.disability`, `coverage.add.dental`, `coverage.add.vision`, `coverage.terminate`, `contribution.change` (update_plan), `billing.change` (update_plan) |
| Channel rules | 14, one per system and change type. A coverage type for a product the platform doesn't hold is an explicit *unsupported* rule, so routing never sends dental work to Platform A. The address change's rules say `applies_to: customer`, so routing makes one target per customer per platform rather than one per plan. **Platform A's address goes through its screen**: its rule names the `browser` channel alone, so TRACE replays the recorded workflow and Platform A's API reads the result back. Every supported change waits for a person's confirmation |
| Employers and plans | **None stored.** Every resolution asks the enterprise plan directory's MCP server (`ROUTING_PLAN_DIRECTORY_URL`) which system hosts each of the customer's plans, live. The customer number is the employer ref, and the plan id is both the plan ref and the system key. If the directory is down, or one platform didn't answer it, routing waits and retries rather than route on part of the answer. Plan imports are refused |
| Workbench `.env` | `ROUTING_PLAN_DIRECTORY_URL=http://127.0.0.1:8213/mcp`; `CONNECTOR_HTTP_ALLOWLIST` lists the six URLs (TRACE's too); `WORKBENCH_SANDBOX_ENABLED=false` |

Every write is recorded in the Workbench's audit log under its first administrator, the same entries its own API writes.

```bash
# with the Workbench's interpreter, from this folder
C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring apply --dry-run   # what would change; writes nothing
C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring apply             # register / update everything (safe to repeat)
C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring check             # routing for sample requests + a read through each server
```

**Keeping the two in step:**
- **After adding, moving or terminating plans on a platform:** nothing to do. The Workbench sees it on the next request, because it asks the directory every time.
- **After changing tools** (or re-recording the TRACE use case, which gives it a new tool name: change it in `wiring/documents.py`): run `wiring apply` again. That covers adding or renaming MCP tools, or changing a tool's arguments. The Workbench refuses a server whose live tools no longer match the manifest it registered.
- **After changing the Workbench's `.env`:** restart the Workbench API, which reads its settings at startup.

**Sentences to try** (each waits for your confirmation before it writes):
- "Harbor Point Manufacturing moved to 1 Riverside Plaza, Columbus OH 43215". This is 2 changes: Harbor Point's customer record on A, through TRACE on A's admin screen (its STD and LTD plans), and on B over its API (its dental and vision plans). TRACE's MCP server must be running.
- "Harbor Point's address is now PO Box 1188, Columbus OH 43216". Platform A takes it; Platform B refuses the PO Box, and Policyholder Services gets the work item.
- "Add STD buy-up for Meadowbrook's salaried employees from 2027-01-01 at 60%, up to $1,200 a week, 7-day elimination"
- "Terminate Harbor Point's STD buy-up for salaried employees effective 2026-12-31, withdrawn at renewal"
- "Make Summit Valley's dental 40% employee-paid, shared"
- "Bill Cedar Ridge quarterly"
