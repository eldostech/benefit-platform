"""The REST API, one app per platform: ``create_app(PLATFORM_A)``.

Contract, the same on both platforms:

* **Auth**: ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``, the
  platform's own key (``PLATFORM_A_API_KEY`` / ``PLATFORM_B_API_KEY``).
  ``/health`` and the OpenAPI docs are open.
* **Errors**: ``{"code", "message", "fields", "correlation_id"}`` with the
  HTTP status; ``code`` is stable (``PLAN_NOT_FOUND``, ``PO_BOX_NOT_ACCEPTED``,
  ...), ``message`` is for people.
* **Writes** take ``Idempotency-Key`` (a repeat returns the first response
  with ``Idempotent-Replay: true``) and ``If-Match: <version>`` (a stale one is
  412). ``GET /plans/{id}`` returns the version as its ``ETag``.
* **Every response** carries ``X-Request-ID`` (yours, or a new one).

No ``from __future__ import annotations`` here: FastAPI evaluates the
routes' annotations at runtime, and some name per-platform values.
"""

import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from platforms.config import Platform
from platforms.db import create_schema, engine
from platforms.schemas import AddressIn, CoverageIn, PlanUpdateIn, TerminateIn
from platforms.service import ApiError, PlatformStore, WriteResult

log = logging.getLogger("platforms.api")

IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=8, max_length=400, description="One key per intended change."),
]
IfMatch = Annotated[
    str | None, Header(alias="If-Match", description="The version you read of the record you change (the plan, or the customer), from its ETag.")
]


def _version(if_match: str | None) -> int | None:
    if if_match is None or if_match.strip() == "*":
        return None
    raw = if_match.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        raise ApiError(400, "INVALID_IF_MATCH", f"If-Match must be a version (an integer), not {if_match!r}.") from None


def _written(result: WriteResult, response: Response) -> dict[str, Any]:
    response.status_code = result.status
    if result.replay:
        response.headers["Idempotent-Replay"] = "true"
    record = result.body.get("plan") or result.body.get("customer") or {}
    if "version" in record:
        response.headers["ETag"] = f'"{record["version"]}"'
    return result.body


def create_app(platform: Platform) -> FastAPI:
    store = PlatformStore(platform)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not platform.api_key:
            # An open API that writes coverage is not a dev convenience; it is
            # the thing a real platform never is.
            raise RuntimeError(f"{platform.api_key_env} is not set; the {platform.name} API will not start without a key")
        create_schema(platform)
        log.info("%s API ready on schema %s", platform.name, platform.schema)
        yield

    app = FastAPI(
        title=f"{platform.name} API",
        version="1.0.0",
        description=(
            f"Plan administration for {platform.name}: product lines {', '.join(platform.product_lines)} only. "
            "Search and read customers and plans, change a customer's mailing address (one per customer, for all its "
            "plans here), change a plan's terms, add and terminate coverages."
        ),
        lifespan=lifespan,
    )

    def authorized(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # An empty X-API-Key is no key, so it cannot shadow a Bearer token.
        supplied = x_api_key or None
        if supplied is None and authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode(), platform.api_key.encode()):
            raise ApiError(401, "UNAUTHORIZED", "Send this platform's API key as X-API-Key or a Bearer token.")

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def _error(request: Request, status: int, code: str, message: str, fields: list) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            {"code": code, "message": message, "fields": fields, "correlation_id": rid},
            status_code=status,
            headers={"X-Request-ID": rid} if rid else None,
        )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(request, exc.status, exc.code, exc.message, exc.fields)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in e.get("loc", ())[1:]) or str(e.get("loc", [""])[0]), "problem": e.get("msg", "")}
            for e in exc.errors()
        ]
        return _error(request, 422, "VALIDATION_FAILED", "The request does not match the API's schema.", fields)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, Any]:
        try:
            with engine().connect() as conn:
                conn.execute(text("select 1"))
            database = True
        except Exception:  # noqa: BLE001 - health reports, it does not raise
            database = False
        return {"status": "ok" if database else "degraded", "platform": platform.code, "database": database}

    guarded = [Depends(authorized)]
    lines = tuple(platform.product_lines)

    @app.get("/api/v1/coverage-options", dependencies=guarded, tags=["catalogue"])
    def coverage_options(
        product_line: Annotated[str | None, Query(description=f"Only this line: {', '.join(lines)}.")] = None,
    ) -> dict[str, Any]:
        """The coverages each product line offers, and the terms each needs."""
        return store.coverage_options(product_line)

    @app.get("/api/v1/customers", dependencies=guarded, tags=["customers"])
    def list_customers(
        q: Annotated[str | None, Query(max_length=80, description="Part of the name, or the start of the number.")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return store.list_customers(q, limit, offset)

    @app.get("/api/v1/customers/{customer_number}", dependencies=guarded, tags=["customers"])
    def get_customer(customer_number: str, response: Response) -> dict[str, Any]:
        """The customer's record on this platform: its mailing address, version and plans."""
        customer = store.get_customer(customer_number)
        response.headers["ETag"] = f'"{customer["version"]}"'
        return customer

    @app.get("/api/v1/customers/{customer_number}/changes", dependencies=guarded, tags=["customers"])
    def customer_changes(customer_number: str, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        """Changes to the customer record itself (its address), newest first."""
        return store.customer_changes(customer_number, limit)

    @app.put("/api/v1/customers/{customer_number}/address", dependencies=guarded, tags=["customers"])
    def update_customer_address(
        customer_number: str, body: AddressIn, response: Response, idempotency_key: IdempotencyKey = None, if_match: IfMatch = None
    ) -> dict[str, Any]:
        """Replace the customer's mailing address on this platform, for every plan it holds here.
        If-Match is the customer's version."""
        result = store.update_customer_address(customer_number, body, idempotency_key=idempotency_key, if_match=_version(if_match))
        return _written(result, response)

    @app.get("/api/v1/plans", dependencies=guarded, tags=["plans"])
    def search_plans(
        customer_number: Annotated[str | None, Query(description="Eight digits starting 00 or 01.")] = None,
        customer_name: Annotated[str | None, Query(max_length=80, description="Part of the customer's legal name.")] = None,
        product_line: Annotated[str | None, Query(description=f"One of {', '.join(lines)}.")] = None,
        status: Literal["active", "terminated"] | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return store.search_plans(
            customer_number=customer_number,
            customer_name=customer_name,
            product_line=product_line,
            status=status,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/plans/{plan_id}", dependencies=guarded, tags=["plans"])
    def get_plan(plan_id: str, response: Response) -> dict[str, Any]:
        plan = store.get_plan(plan_id)
        response.headers["ETag"] = f'"{plan["version"]}"'
        return plan

    @app.get("/api/v1/plans/{plan_id}/changes", dependencies=guarded, tags=["plans"])
    def plan_changes(plan_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        return store.plan_changes(plan_id, limit)

    @app.patch("/api/v1/plans/{plan_id}", dependencies=guarded, tags=["plans"])
    def update_plan(
        plan_id: str, body: PlanUpdateIn, response: Response, idempotency_key: IdempotencyKey = None, if_match: IfMatch = None
    ) -> dict[str, Any]:
        """Change a plan's name, renewal date, billing frequency or contribution. The mailing address is the customer's."""
        result = store.update_plan(
            plan_id, body.model_dump(mode="json", exclude_unset=True), idempotency_key=idempotency_key, if_match=_version(if_match)
        )
        return _written(result, response)

    @app.post("/api/v1/plans/{plan_id}/coverages", status_code=201, dependencies=guarded, tags=["coverages"])
    def add_coverage(
        plan_id: str, body: CoverageIn, response: Response, idempotency_key: IdempotencyKey = None, if_match: IfMatch = None
    ) -> dict[str, Any]:
        """Add a coverage to the plan; the code must be one its product line offers."""
        result = store.add_coverage(plan_id, body, idempotency_key=idempotency_key, if_match=_version(if_match))
        return _written(result, response)

    @app.post("/api/v1/plans/{plan_id}/coverages/{coverage_id}/terminate", dependencies=guarded, tags=["coverages"])
    def terminate_coverage(
        plan_id: str,
        coverage_id: str,
        body: TerminateIn,
        response: Response,
        idempotency_key: IdempotencyKey = None,
        if_match: IfMatch = None,
    ) -> dict[str, Any]:
        """End a coverage on a date. The record stays, with its termination date and reason."""
        result = store.terminate_coverage(plan_id, coverage_id, body, idempotency_key=idempotency_key, if_match=_version(if_match))
        return _written(result, response)

    return app
