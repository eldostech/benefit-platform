"""The enterprise plan directory API: ``create_directory_app()``.

One API over both admin systems, answering the routing question -- *which
system hosts each of this customer's plans?* -- from the systems themselves.
It stores nothing: every call asks Platform A and Platform B concurrently,
through their own REST APIs and keys, and merges what they say, each plan
labelled with the ``admin_system`` that hosts it.

**An answer says whether it is whole.** If a platform does not answer, the
directory still returns what the others said, with ``complete: false`` and
that platform under ``sources`` as ``unavailable``. A caller that routes work
must not read an incomplete answer as "the customer has no plans there"; the
Operation Workbench refuses to route on one and says why.

A platform that does not hold a product line answers a search for it with
``422 PRODUCT_NOT_OFFERED``; for the directory that is an empty answer from
that platform, not a failure.

Auth is the directory's own key (``PLAN_DIRECTORY_API_KEY``), as
``X-API-Key`` or ``Authorization: Bearer``; ``/health`` and ``/docs`` are open.
"""

import asyncio
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from platforms.config import CUSTOMER_NUMBER, DIRECTORY, Directory, Platform

log = logging.getLogger("platforms.directory")

PAGE = 100


class DirectoryError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields or []


@dataclass
class _Answer:
    """One platform's part of an answer."""

    platform: Platform
    status: str  # ok | unavailable | error
    items: list[dict[str, Any]]
    detail: str = ""

    def source(self) -> dict[str, Any]:
        return {"admin_system": self.platform.code, "name": self.platform.name, "status": self.status, "detail": self.detail or None}


def _location(platform: Platform, plan: dict[str, Any]) -> dict[str, Any]:
    """A plan as the directory reports it: where it lives and what routing
    needs to choose it -- never its terms or coverages, nor the customer's
    address, which only the platform answers for."""
    return {
        "plan_id": plan["plan_id"],
        "admin_system": platform.code,
        "customer_number": plan["customer_number"],
        "customer_name": plan["customer_name"],
        "product_line": plan["product_line"],
        "plan_name": plan["plan_name"],
        "status": plan["status"],
        "effective_date": plan["effective_date"],
        "termination_date": plan["termination_date"],
    }


def _check_number(number: str) -> None:
    if not CUSTOMER_NUMBER.match(number):
        raise DirectoryError(
            422,
            "INVALID_CUSTOMER_NUMBER",
            f"{number!r} is not a customer number: eight digits starting 00 or 01, e.g. 00104217.",
            [{"field": "customer_number", "problem": "eight digits starting 00 or 01"}],
        )


class PlanDirectory:
    """The federation itself, over one HTTP client per platform."""

    def __init__(self, directory: Directory, clients: dict[str, httpx.AsyncClient] | None = None) -> None:
        self.d = directory
        self._given = clients or {}
        self._own: dict[str, httpx.AsyncClient] = {}

    def client(self, p: Platform) -> httpx.AsyncClient:
        if p.code in self._given:
            return self._given[p.code]
        if p.code not in self._own:
            self._own[p.code] = httpx.AsyncClient(
                base_url=p.api_base_url, headers={"X-API-Key": p.api_key}, timeout=httpx.Timeout(15.0, connect=3.0)
            )
        return self._own[p.code]

    async def aclose(self) -> None:
        for c in self._own.values():
            await c.aclose()

    async def _each(self, ask) -> list[_Answer]:
        """Ask every platform at once; one that fails is reported, never fatal."""

        async def one(p: Platform) -> _Answer:
            try:
                return await ask(p)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                log.warning("%s did not answer the directory: %r", p.code, exc)
                return _Answer(p, "unavailable", [], f"{p.name} did not answer: {type(exc).__name__}")

        return list(await asyncio.gather(*(one(p) for p in self.d.sources)))

    async def _plans_from(self, p: Platform, params: dict[str, Any]) -> _Answer:
        items: list[dict[str, Any]] = []
        while True:
            resp = await self.client(p).get("/api/v1/plans", params={**params, "limit": PAGE, "offset": len(items)})
            if resp.status_code == 422 and resp.json().get("code") == "PRODUCT_NOT_OFFERED":
                return _Answer(p, "ok", [])
            if resp.status_code >= 400:
                return _Answer(p, "error", [], f"{p.name} answered {resp.status_code} {resp.json().get('code', '')}".strip())
            body = resp.json()
            items.extend(_location(p, plan) for plan in body["items"])
            if len(items) >= body["total"] or not body["items"]:
                return _Answer(p, "ok", items)

    @staticmethod
    def _envelope(answers: list[_Answer], **data: Any) -> dict[str, Any]:
        return {**data, "complete": all(a.status == "ok" for a in answers), "sources": [a.source() for a in answers]}

    @staticmethod
    def _customers(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_number: dict[str, dict[str, Any]] = {}
        for plan in plans:
            c = by_number.setdefault(
                plan["customer_number"], {"customer_number": plan["customer_number"], "legal_name": plan["customer_name"], "hosted_on": []}
            )
            if plan["admin_system"] not in c["hosted_on"]:
                c["hosted_on"].append(plan["admin_system"])
        return sorted(by_number.values(), key=lambda c: c["customer_number"])

    async def search_plans(
        self,
        *,
        customer_number: str | None = None,
        customer_name: str | None = None,
        product_line: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if not (customer_number or customer_name):
            raise DirectoryError(
                422, "CUSTOMER_REQUIRED", "Name the customer: customer_number, or part of customer_name.", [{"field": "customer_number", "problem": "or customer_name"}]
            )
        if customer_number:
            _check_number(customer_number)
        params = {
            k: v
            for k, v in {"customer_number": customer_number, "customer_name": customer_name, "product_line": product_line, "status": status}.items()
            if v
        }
        answers = await self._each(lambda p: self._plans_from(p, params))
        plans = sorted((plan for a in answers for plan in a.items), key=lambda x: (x["customer_number"], x["admin_system"], x["plan_id"]))
        return self._envelope(answers, plans=plans, customers=self._customers(plans))

    async def search_customers(self, q: str) -> dict[str, Any]:
        async def ask(p: Platform) -> _Answer:
            resp = await self.client(p).get("/api/v1/customers", params={"q": q, "limit": PAGE})
            if resp.status_code >= 400:
                return _Answer(p, "error", [], f"{p.name} answered {resp.status_code}")
            return _Answer(p, "ok", [{**c, "admin_system": p.code} for c in resp.json()["items"]])

        answers = await self._each(ask)
        merged: dict[str, dict[str, Any]] = {}
        for a in answers:
            for c in a.items:
                m = merged.setdefault(c["customer_number"], {"customer_number": c["customer_number"], "legal_name": c["legal_name"], "hosted_on": []})
                m["hosted_on"].append(c["admin_system"])
        return self._envelope(answers, customers=sorted(merged.values(), key=lambda c: c["customer_number"]))

    async def get_customer(self, customer_number: str) -> dict[str, Any]:
        found = await self.search_plans(customer_number=customer_number)
        if not found["customers"]:
            if not found["complete"]:
                raise DirectoryError(503, "DIRECTORY_INCOMPLETE", self._missing(found, f"customer {customer_number}"))
            raise DirectoryError(404, "CUSTOMER_NOT_FOUND", f"No system hosts a customer {customer_number}.")
        (customer,) = found["customers"]
        return {**customer, "plans": found["plans"], "complete": found["complete"], "sources": found["sources"]}

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        async def ask(p: Platform) -> _Answer:
            resp = await self.client(p).get(f"/api/v1/plans/{plan_id}")
            if resp.status_code == 404:
                return _Answer(p, "ok", [])
            if resp.status_code >= 400:
                return _Answer(p, "error", [], f"{p.name} answered {resp.status_code}")
            return _Answer(p, "ok", [_location(p, resp.json())])

        answers = await self._each(ask)
        hits = [plan for a in answers for plan in a.items]
        if not hits:
            if not all(a.status == "ok" for a in answers):
                raise DirectoryError(503, "DIRECTORY_INCOMPLETE", self._missing(self._envelope(answers), f"plan {plan_id}"))
            raise DirectoryError(404, "PLAN_NOT_FOUND", f"No system hosts a plan {plan_id}.")
        return {**hits[0], "complete": all(a.status == "ok" for a in answers), "sources": [a.source() for a in answers]}

    @staticmethod
    def _missing(envelope: dict[str, Any], what: str) -> str:
        down = [s["name"] for s in envelope["sources"] if s["status"] != "ok"]
        return f"{', '.join(down)} did not answer, so the directory cannot say where {what} is."


def create_directory_app(directory: Directory = DIRECTORY, *, clients: dict[str, httpx.AsyncClient] | None = None) -> FastAPI:
    federation = PlanDirectory(directory, clients)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not directory.api_key:
            raise RuntimeError(f"{directory.api_key_env} is not set; the {directory.name} will not start without a key")
        missing = [p.api_key_env for p in directory.sources if not p.api_key and p.code not in (clients or {})]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} not set; the directory cannot ask those platforms")
        yield
        await federation.aclose()

    app = FastAPI(
        title=f"{directory.name} API",
        version="1.0.0",
        description=(
            "Which admin system hosts each of a customer's plans, asked live of every system: "
            + ", ".join(f"{p.name}" for p in directory.sources)
            + ". Holds no data. An answer with complete=false is missing a system that did not answer."
        ),
        lifespan=lifespan,
    )

    def authorized(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        supplied = x_api_key or None
        if supplied is None and authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode(), directory.api_key.encode()):
            raise DirectoryError(401, "UNAUTHORIZED", "Send the directory's API key as X-API-Key or a Bearer token.")

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(DirectoryError)
    async def refused(request: Request, exc: DirectoryError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "fields": exc.fields, "correlation_id": rid},
            status_code=exc.status,
            headers={"X-Request-ID": rid} if rid else None,
        )

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        async def ask(p: Platform) -> _Answer:
            resp = await federation.client(p).get("/health")
            return _Answer(p, "ok" if resp.status_code == 200 else "error", [])

        answers = await federation._each(ask)
        whole = all(a.status == "ok" for a in answers)
        return {"status": "ok" if whole else "degraded", "directory": directory.code, "sources": [a.source() for a in answers]}

    guarded = [Depends(authorized)]

    @app.get("/api/v1/plans", dependencies=guarded, tags=["plans"])
    async def search_plans(
        customer_number: Annotated[str | None, Query(description="Eight digits starting 00 or 01.")] = None,
        customer_name: Annotated[str | None, Query(max_length=80, description="Part of the customer's legal name.")] = None,
        product_line: Annotated[str | None, Query(description="STD, LTD, DENTAL or VISION.")] = None,
        status: Literal["active", "terminated"] | None = None,
    ) -> dict[str, Any]:
        """Every plan of the customer, on every system, each with the system that hosts it."""
        return await federation.search_plans(
            customer_number=customer_number, customer_name=customer_name, product_line=product_line, status=status
        )

    @app.get("/api/v1/plans/{plan_id}", dependencies=guarded, tags=["plans"])
    async def get_plan(plan_id: str) -> dict[str, Any]:
        """Which system hosts one plan."""
        return await federation.get_plan(plan_id)

    @app.get("/api/v1/customers", dependencies=guarded, tags=["customers"])
    async def search_customers(q: Annotated[str, Query(min_length=2, max_length=80, description="Part of the name, or the start of the number.")]) -> dict[str, Any]:
        """Customers whose name or number matches, and the systems each is on."""
        return await federation.search_customers(q)

    @app.get("/api/v1/customers/{customer_number}", dependencies=guarded, tags=["customers"])
    async def get_customer(customer_number: str) -> dict[str, Any]:
        """One customer, the systems it is on, and every plan it holds there."""
        return await federation.get_customer(customer_number)

    return app
