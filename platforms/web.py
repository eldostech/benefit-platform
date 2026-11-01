"""A platform's administration screens: what a person uses, and what a
browser agent drives for a platform that takes no API calls.

The screens are a third door onto the same `PlatformStore` the REST API
uses, so every rule holds here as it does there -- the product lines, the PO
Box refusal, the version check, the change log -- and a change made on a
screen carries a confirmation number from the same sequence (``A-CHG-…``).
Nothing here is a second implementation of a business rule.

The mailing address is the customer's: it is changed on the customer's page
(``/customers/<number>/address``), once for every plan the customer holds on
this platform. A plan's page shows it and links there.

**No sign-in.** These are test screens for the Operation Workbench: anyone
who can reach the port can read and change anything. Never expose them.

Built to be driven by a browser as well as used by a person:

* **Server-rendered forms, no script.** Every page is whole after one load;
  there is nothing to wait for but the page itself.
* **Stable handles.** Every control has a ``<label for>``, an ``id`` and a
  ``data-testid``; every table row is ``data-testid="…-row-<id>"``; the
  confirmation number is ``data-testid="confirmation-number"``.
* **A refusal is on the page.** A field the platform refused is marked
  ``aria-invalid`` with its message beside it, and the page's banner is
  ``role="alert"`` -- the same codes the API answers (``PO_BOX_NOT_ACCEPTED``),
  in ``data-error-code``.
* **A resubmitted form is not a second change.** Each form carries a token
  sent as the write's ``Idempotency-Key``, and the version of the record it
  changes (the plan, or the customer for the address) as its ``If-Match``: a double click answers with the first change, and a form
  opened before someone else's change is refused, not applied over it.
* **Terminating a coverage asks twice**: a review page, then Confirm.

No ``from __future__ import annotations``: FastAPI reads the handlers'
annotations at runtime.
"""

import uuid
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from platforms.config import BILLING_FREQUENCIES, CONTRIBUTION_BASES, CUSTOMER_NUMBER, EMPLOYEE_CLASSES, Platform
from platforms.schemas import AddressIn, CoverageIn, PlanUpdateIn, TerminateIn
from platforms.service import US_STATES, ApiError, PlatformStore

TEMPLATES = Path(__file__).resolve().parent / "templates"
PAGE = 25
PLAN_FIELDS = ("plan_name", "renewal_date", "billing_frequency", "employee_contribution_pct", "contribution_basis")


def _field_errors(exc: ApiError) -> dict[str, str]:
    """Field name -> message, for marking the inputs a refusal names."""
    return {f["field"]: f["problem"] for f in exc.fields}


def _validation_error(exc: ValidationError) -> ApiError:
    fields = [
        {"field": ".".join(str(part) for part in e["loc"]), "problem": e["msg"].removeprefix("Value error, ")} for e in exc.errors()
    ]
    return ApiError(
        422, "INVALID_INPUT", "Some fields need attention: " + "; ".join(f"{f['field']}: {f['problem']}" for f in fields) + ".", fields
    )


def _term_value(kind: str, raw: str) -> Any:
    """A term as typed, in the type its kind needs; left as text when it is
    not one, so the platform's own check names the problem."""
    raw = raw.strip()
    try:
        if kind == "int_range":
            return int(raw)
        if kind == "num_range":
            number = float(raw)
            return int(number) if number.is_integer() else number
    except ValueError:
        return raw
    return int(raw) if raw.lstrip("-").isdigit() else raw


def _write_keys(form: dict[str, str]) -> dict[str, Any]:
    version = form.get("version", "")
    return {
        "idempotency_key": f"web-{form['form_id']}" if form.get("form_id") else None,
        "if_match": int(version) if version.isdigit() else None,
    }


def _written(result: Any, back: str) -> RedirectResponse:
    """To the change's confirmation, or back to the record's page (``back``)
    when nothing changed."""
    change_id = result.body.get("change_id")
    if change_id is None:
        return RedirectResponse(f"{back}?notice=unchanged", status_code=303)
    return RedirectResponse(f"/changes/{quote(change_id)}?saved=1", status_code=303)


def _plan_page(plan_id: str) -> str:
    return f"/plans/{quote(plan_id)}"


async def _form(request: Request) -> dict[str, str]:
    return {k: str(v).strip() for k, v in (await request.form()).items()}


def create_web_app(platform: Platform, store: PlatformStore | None = None) -> FastAPI:
    store = store or PlatformStore(platform)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    app = FastAPI(title=platform.web_title or platform.name, docs_url=None, redoc_url=None, openapi_url=None)

    def render(request: Request, template: str, http_status: int = 200, **context: Any) -> HTMLResponse:
        page = env.get_template(template).render(platform=platform, path=request.url.path, today=date.today().isoformat(), **context)
        return HTMLResponse(page, status_code=http_status, headers={"Cache-Control": "no-store"})

    def form_page(request: Request, template: str, error: ApiError | None, values: dict[str, str], **context: Any) -> HTMLResponse:
        """A form, fresh or refused: a refusal keeps what was typed (and the
        form's token, so resubmitting it is still the same change)."""
        return render(
            request,
            template,
            http_status=error.status if error else 200,
            values=values,
            error=error,
            fields=_field_errors(error) if error else {},
            form_id=values.get("form_id") or uuid.uuid4().hex,
            **context,
        )

    @app.exception_handler(ApiError)
    async def _refused(request: Request, exc: ApiError) -> Response:
        return render(request, "error.html", http_status=exc.status, error=exc)

    @app.exception_handler(404)
    async def _missing(request: Request, _: Exception) -> Response:
        return render(request, "error.html", http_status=404, error=ApiError(404, "NOT_FOUND", "There is no such page."))

    # ------------------------------------------------------------------ reads
    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "platform": platform.code, "screens": True})

    @app.get("/")
    def home() -> Response:
        return RedirectResponse("/plans", status_code=303)

    @app.get("/plans")
    def plans(request: Request, q: str = "", product_line: str = "", status: str = "", page: int = 1) -> HTMLResponse:
        q = q.strip()
        page = max(1, page)
        number = bool(CUSTOMER_NUMBER.match(q))
        results = store.search_plans(
            customer_number=q if number else None,
            customer_name=q if q and not number else None,
            product_line=product_line or None,
            status=status or None,
            limit=PAGE,
            offset=(page - 1) * PAGE,
        )
        return render(
            request,
            "plans.html",
            q=q,
            product_line=product_line,
            status=status,
            results=results,
            page=page,
            pages=max(1, -(-results["total"] // PAGE)),
            filters=urlencode({k: v for k, v in {"q": q, "product_line": product_line, "status": status}.items() if v}),
        )

    @app.get("/customers")
    def customers(request: Request, q: str = "") -> HTMLResponse:
        return render(request, "customers.html", q=q.strip(), results=store.list_customers(q.strip() or None, 100, 0))

    @app.get("/customers/{customer_number}")
    def customer(request: Request, customer_number: str, notice: str = "") -> HTMLResponse:
        return render(
            request,
            "customer.html",
            customer=store.get_customer(customer_number),
            changes=store.customer_changes(customer_number, 20)["items"],
            notice=notice,
        )

    @app.get("/plans/{plan_id}")
    def plan(request: Request, plan_id: str, notice: str = "") -> HTMLResponse:
        plan_ = store.get_plan(plan_id)
        return render(
            request,
            "plan.html",
            plan=plan_,
            customer=store.get_customer(plan_["customer_number"]),
            changes=store.plan_changes(plan_id, 20)["items"],
            notice=notice,
        )

    @app.get("/changes/{change_id}")
    def change(request: Request, change_id: str, saved: int = 0) -> HTMLResponse:
        return render(request, "change.html", change=store.change(change_id), saved=bool(saved))

    # ----------------------------------------------------------------- address
    def address_page(
        request: Request, customer_: dict[str, Any], values: dict[str, str] | None = None, error: ApiError | None = None
    ) -> HTMLResponse:
        current = customer_["address"]
        values = values or {k: current.get(k) or "" for k in ("line1", "line2", "city", "state", "postal_code")}
        return form_page(request, "address.html", error, values, customer=customer_, states=sorted(US_STATES))

    @app.get("/customers/{customer_number}/address")
    def address_form(request: Request, customer_number: str) -> HTMLResponse:
        return address_page(request, store.get_customer(customer_number))

    @app.post("/customers/{customer_number}/address")
    async def address_save(request: Request, customer_number: str) -> Response:
        form = await _form(request)
        values = {k: form.get(k, "") for k in ("line1", "line2", "city", "state", "postal_code", "form_id")}
        try:
            address = AddressIn(**{k: values[k] for k in ("line1", "city", "state", "postal_code")}, line2=values["line2"] or None)
            result = store.update_customer_address(customer_number, address, **_write_keys(form))
        except ValidationError as exc:
            return address_page(request, store.get_customer(customer_number), values, _validation_error(exc))
        except ApiError as exc:
            if exc.status == 404:
                raise
            return address_page(request, store.get_customer(customer_number), values, exc)
        return _written(result, f"/customers/{quote(customer_number)}")

    @app.get("/plans/{plan_id}/address")
    def plan_address(plan_id: str) -> Response:
        """The address is the customer's: an old link to a plan's address
        form lands on the customer's."""
        number = store.get_plan(plan_id)["customer_number"]
        return RedirectResponse(f"/customers/{quote(number)}/address", status_code=303)

    # ------------------------------------------------------------ plan details
    def details_page(request: Request, plan_: dict[str, Any], values: dict[str, str] | None = None, error: ApiError | None = None) -> HTMLResponse:
        values = values or {k: "" if plan_.get(k) is None else str(plan_[k]) for k in PLAN_FIELDS}
        return form_page(request, "details.html", error, values, plan=plan_, frequencies=BILLING_FREQUENCIES, bases=CONTRIBUTION_BASES)

    @app.get("/plans/{plan_id}/details")
    def details_form(request: Request, plan_id: str) -> HTMLResponse:
        return details_page(request, store.get_plan(plan_id))

    @app.post("/plans/{plan_id}/details")
    async def details_save(request: Request, plan_id: str) -> Response:
        form = await _form(request)
        current = store.get_plan(plan_id)
        values = {k: form.get(k, "") for k in (*PLAN_FIELDS, "form_id")}
        # Only what was changed on the form is sent, as an API caller would.
        sent = {k: values[k] for k in PLAN_FIELDS if values[k] and values[k] != ("" if current.get(k) is None else str(current[k]))}
        if not sent:
            return RedirectResponse(f"{_plan_page(plan_id)}?notice=unchanged", status_code=303)
        try:
            body = PlanUpdateIn(**sent)
            result = store.update_plan(plan_id, body.model_dump(mode="json", exclude_none=True), **_write_keys(form))
        except ValidationError as exc:
            return details_page(request, current, values, _validation_error(exc))
        except ApiError as exc:
            if exc.status == 404:
                raise
            return details_page(request, current, values, exc)
        return _written(result, _plan_page(plan_id))

    # ---------------------------------------------------------- add a coverage
    def coverage_page(
        request: Request, plan_: dict[str, Any], code: str, values: dict[str, str] | None = None, error: ApiError | None = None
    ) -> HTMLResponse:
        options = platform.coverage_catalog[plan_["product_line"]]
        values = values or {"employee_class": "ALL", "effective_date": plan_["effective_date"]}
        return form_page(
            request,
            "coverage_new.html",
            error,
            values,
            plan=plan_,
            choices=[(o.code, f"{o.name} ({o.code})") for o in options],
            option=next((o for o in options if o.code == code), None),
            classes=EMPLOYEE_CLASSES,
        )

    @app.get("/plans/{plan_id}/coverages/new")
    def coverage_form(request: Request, plan_id: str, coverage_code: str = "") -> HTMLResponse:
        return coverage_page(request, store.get_plan(plan_id), coverage_code.upper())

    @app.post("/plans/{plan_id}/coverages/new")
    async def coverage_save(request: Request, plan_id: str) -> Response:
        form = await _form(request)
        plan_ = store.get_plan(plan_id)
        code = form.get("coverage_code", "").upper()
        option = platform.option(plan_["product_line"], code)
        if option is None:
            error = ApiError(422, "PRODUCT_NOT_OFFERED", "Choose a coverage this plan can carry.", [{"field": "coverage_code", "problem": "required"}])
            return coverage_page(request, plan_, "", form, error)
        terms = {name: _term_value(kind, form[f"term_{name}"]) for name, (kind, _) in option.terms.items() if form.get(f"term_{name}")}
        try:
            body = CoverageIn(
                coverage_code=code, employee_class=form.get("employee_class") or "ALL", effective_date=form.get("effective_date", ""), terms=terms
            )
            result = store.add_coverage(plan_id, body, **_write_keys(form))
        except ValidationError as exc:
            return coverage_page(request, plan_, code, form, _validation_error(exc))
        except ApiError as exc:
            if exc.status == 404:
                raise
            return coverage_page(request, plan_, code, form, exc)
        return _written(result, _plan_page(plan_id))

    # ---------------------------------------------------- terminate a coverage
    def coverage_of(plan_: dict[str, Any], coverage_id: str) -> dict[str, Any]:
        found = next((c for c in plan_["coverages"] if c["coverage_id"] == coverage_id), None)
        if found is None:
            raise ApiError(404, "COVERAGE_NOT_FOUND", f"Plan {plan_['plan_id']} has no coverage {coverage_id}.")
        return found

    def terminate_page(
        request: Request, plan_: dict[str, Any], coverage: dict[str, Any], values: dict[str, str] | None = None, error: ApiError | None = None, review: bool = False
    ) -> HTMLResponse:
        values = values or {"termination_date": "", "reason": ""}
        return form_page(request, "terminate_review.html" if review else "terminate.html", error, values, plan=plan_, coverage=coverage)

    @app.get("/plans/{plan_id}/coverages/{coverage_id}/terminate")
    def terminate_form(request: Request, plan_id: str, coverage_id: str) -> HTMLResponse:
        plan_ = store.get_plan(plan_id)
        return terminate_page(request, plan_, coverage_of(plan_, coverage_id))

    @app.post("/plans/{plan_id}/coverages/{coverage_id}/terminate")
    async def terminate_save(request: Request, plan_id: str, coverage_id: str) -> Response:
        form = await _form(request)
        plan_ = store.get_plan(plan_id)
        coverage = coverage_of(plan_, coverage_id)
        values = {k: form.get(k, "") for k in ("termination_date", "reason", "form_id")}
        try:
            body = TerminateIn(termination_date=values["termination_date"], reason=values["reason"])
        except ValidationError as exc:
            return terminate_page(request, plan_, coverage, values, _validation_error(exc))
        # The review page states what will happen; only its Confirm writes.
        if form.get("step") != "confirm":
            return terminate_page(request, plan_, coverage, values, review=True)
        try:
            result = store.terminate_coverage(plan_id, coverage_id, body, **_write_keys(form))
        except ApiError as exc:
            if exc.status == 404:
                raise
            return terminate_page(request, plan_, coverage, values, exc)
        return _written(result, _plan_page(plan_id))

    return app


__all__ = ["create_web_app"]
