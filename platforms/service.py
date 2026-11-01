"""The business rules both platforms run: reads, the four writes, and the
change log every write leaves.

**The mailing address is the customer's**, not a plan's: a customer has one
address on each platform, whatever plans it holds there, so moving a
customer that is on both platforms is one change on each. A plan change
(terms, coverages) is made to the plan, with the plan's version; an address
change is made to the customer, with the customer's version.

Every write goes through `PlatformStore._write`, which is where the
guarantees a caller relies on live, in this order and in one transaction:

1. the row written -- the plan, or the customer -- is locked (``FOR
   UPDATE``), so two writers to one record queue instead of both reading
   the old version;
2. an ``Idempotency-Key`` seen before answers with the response it got the
   first time -- if the request is the same; the same key with a different
   request is refused, because silently applying either would be a guess;
   the lookup happens *after* the lock so a key racing itself on one record
   is decided by the first writer to commit;
3. ``If-Match`` (the record's version) is checked, so a caller that read an
   older one is told to read again rather than overwrite what it never saw;
4. a terminated plan, or an inactive customer, takes no changes;
5. the change is applied, the version bumped, and a change record written
   holding the record before and after and the exact response returned.

A write that changes nothing (the same address again) answers 200 with
``change_id: null`` and leaves no record and no version bump, so a client
that retries without a key cannot inflate the history.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import Connection, Row, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from platforms.config import CUSTOMER_NUMBER, CoverageOption, Platform
from platforms.db import engine, next_id, tables
from platforms.schemas import AddressIn, CoverageIn, TerminateIn

US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
POSTAL_CODE = re.compile(r"^[0-9]{5}(-[0-9]{4})?$")
PO_BOX = re.compile(r"\bP\.?\s*O\.?\s*BOX\b|\bPOST\s+OFFICE\s+BOX\b", re.IGNORECASE)


class ApiError(Exception):
    """A refusal the API returns as ``{code, message, fields}`` with ``status``."""

    def __init__(self, status: int, code: str, message: str, fields: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or []


@dataclass(frozen=True)
class WriteResult:
    status: int
    body: dict[str, Any]
    replay: bool = False


@dataclass
class _Outcome:
    status: int
    changed: list[str]
    previous: dict[str, Any] | None
    extra: dict[str, Any] = field(default_factory=dict)


def _j(value: Any) -> Any:
    """A column value as JSON: dates as ISO strings, numerics as floats."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _term_doc(kind: str, allowed: tuple) -> dict[str, Any]:
    if kind == "int_range":
        return {"type": "integer", "minimum": allowed[0], "maximum": allowed[1]}
    if kind == "num_range":
        return {"type": "number", "minimum": allowed[0], "maximum": allowed[1]}
    return {"enum": list(allowed)}


def _check_terms(option: CoverageOption, terms: dict[str, Any]) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    for name in sorted(set(terms) - set(option.terms)):
        problems.append({"field": f"terms.{name}", "problem": f"{option.code} has no term {name!r}"})
    clean: dict[str, Any] = {}
    for name, (kind, allowed) in option.terms.items():
        if name not in terms:
            problems.append({"field": f"terms.{name}", "problem": "required"})
            continue
        value = terms[name]
        if kind == "int_range":
            if isinstance(value, bool) or not isinstance(value, int) or not allowed[0] <= value <= allowed[1]:
                problems.append({"field": f"terms.{name}", "problem": f"a whole number from {allowed[0]} to {allowed[1]}"})
                continue
        elif kind == "num_range":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not allowed[0] <= value <= allowed[1]:
                problems.append({"field": f"terms.{name}", "problem": f"a number from {allowed[0]} to {allowed[1]}"})
                continue
        elif value not in allowed or isinstance(value, bool):
            problems.append({"field": f"terms.{name}", "problem": f"one of {', '.join(map(str, allowed))}"})
            continue
        clean[name] = value
    if problems:
        raise ApiError(422, "INVALID_TERMS", f"The terms do not fit {option.code}.", problems)
    return clean


class PlatformStore:
    def __init__(self, platform: Platform) -> None:
        self.p = platform
        self.t = tables(platform)

    # ------------------------------------------------------------------ reads
    def coverage_options(self, product_line: str | None = None) -> dict[str, Any]:
        line = product_line.upper() if product_line else None
        if line is not None and line not in self.p.product_lines:
            raise ApiError(
                422,
                "PRODUCT_NOT_OFFERED",
                f"{self.p.name} holds {' and '.join(self.p.product_lines)} plans only, never {line}.",
                [{"field": "product_line", "problem": f"one of {', '.join(self.p.product_lines)}"}],
            )
        return {
            "platform": self.p.code,
            "product_lines": [
                {
                    "product_line": line_,
                    "coverages": [
                        {"coverage_code": o.code, "name": o.name, "terms": {k: _term_doc(*v) for k, v in o.terms.items()}}
                        for o in options
                    ],
                }
                for line_, options in self.p.coverage_catalog.items()
                if line is None or line_ == line
            ],
        }

    def list_customers(self, q: str | None, limit: int, offset: int) -> dict[str, Any]:
        """Customers by part of the name or the start of the number, each with
        the products it holds here, so a search answers "what does X have"."""
        c, pl = self.t.customers, self.t.plans
        where = []
        if q:
            where.append(or_(c.c.legal_name.ilike(f"%{q}%"), c.c.customer_number.startswith(q)))
        with engine().connect() as conn:
            total = conn.execute(select(func.count()).select_from(c).where(*where)).scalar_one()
            rows = conn.execute(select(c).where(*where).order_by(c.c.customer_number).limit(limit).offset(offset)).all()
            plans: dict[str, list[Row]] = {}
            numbers = [r.customer_number for r in rows]
            if numbers:
                for p in conn.execute(select(pl).where(pl.c.customer_number.in_(numbers)).order_by(pl.c.plan_id)).all():
                    plans.setdefault(p.customer_number, []).append(p)
        return {
            "items": [
                {
                    "customer_number": r.customer_number,
                    "legal_name": r.legal_name,
                    "status": r.status,
                    "address": self._address(r),
                    "products": self._products(plans.get(r.customer_number, [])),
                    "plans": [
                        {"plan_id": p.plan_id, "product_line": p.product_line, "plan_name": p.plan_name, "status": p.status}
                        for p in plans.get(r.customer_number, [])
                    ],
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_customer(self, customer_number: str) -> dict[str, Any]:
        self._check_customer_number(customer_number)
        with engine().connect() as conn:
            return self._customer_json(conn, self._customer_row(conn, customer_number))

    def customer_changes(self, customer_number: str, limit: int = 50) -> dict[str, Any]:
        """The changes made to the customer record itself (its address), newest
        first. A plan's own changes are in its history."""
        self._check_customer_number(customer_number)
        ch = self.t.changes
        with engine().connect() as conn:
            self._customer_row(conn, customer_number)
            rows = conn.execute(
                select(ch)
                .where(ch.c.customer_number == customer_number, ch.c.plan_id.is_(None))
                .order_by(ch.c.created_at.desc(), ch.c.change_id.desc())
                .limit(limit)
            ).all()
        return {"customer_number": customer_number, "items": [self._history_row(r) for r in rows]}

    def search_plans(
        self,
        *,
        customer_number: str | None = None,
        customer_name: str | None = None,
        product_line: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        pl, c, cov = self.t.plans, self.t.customers, self.t.coverages
        where = []
        if customer_number:
            self._check_customer_number(customer_number)
            where.append(pl.c.customer_number == customer_number)
        if customer_name:
            where.append(c.c.legal_name.ilike(f"%{customer_name}%"))
        if product_line:
            line = product_line.upper()
            if line not in self.p.product_lines:
                raise ApiError(
                    422,
                    "PRODUCT_NOT_OFFERED",
                    f"{self.p.name} holds {' and '.join(self.p.product_lines)} plans only, never {line}.",
                    [{"field": "product_line", "problem": f"one of {', '.join(self.p.product_lines)}"}],
                )
            where.append(pl.c.product_line == line)
        if status:
            where.append(pl.c.status == status)
        active = (
            select(func.count())
            .where(cov.c.plan_id == pl.c.plan_id, cov.c.status == "active")
            .scalar_subquery()
            .label("active_coverages")
        )
        base = select(pl, c.c.legal_name, active).join(c, c.c.customer_number == pl.c.customer_number).where(*where)
        with engine().connect() as conn:
            total = conn.execute(select(func.count()).select_from(base.subquery())).scalar_one()
            rows = conn.execute(base.order_by(pl.c.customer_number, pl.c.plan_id).limit(limit).offset(offset)).all()
        items = []
        for r in rows:
            summary = self._plan_fields(r, r.legal_name)
            summary["active_coverages"] = r.active_coverages
            items.append(summary)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with engine().connect() as conn:
            return self._plan_json(conn, self._plan_row(conn, plan_id))

    def plan_changes(self, plan_id: str, limit: int = 50) -> dict[str, Any]:
        ch = self.t.changes
        with engine().connect() as conn:
            self._plan_row(conn, plan_id)
            rows = conn.execute(
                select(ch).where(ch.c.plan_id == plan_id).order_by(ch.c.created_at.desc(), ch.c.change_id.desc()).limit(limit)
            ).all()
        return {"plan_id": plan_id, "items": [self._history_row(r) for r in rows]}

    def change(self, change_id: str) -> dict[str, Any]:
        """One change record, by its confirmation number. ``plan_id`` is null
        for a change to the customer record."""
        ch = self.t.changes
        with engine().connect() as conn:
            row = conn.execute(select(ch).where(ch.c.change_id == change_id)).first()
        if row is None:
            raise ApiError(404, "CHANGE_NOT_FOUND", f"No change {change_id} on {self.p.name}.")
        return {
            "change_id": row.change_id,
            "customer_number": row.customer_number,
            "plan_id": row.plan_id,
            "operation": row.operation,
            "changed": row.response.get("changed", []),
            "previous": row.response.get("previous"),
            "coverage": row.response.get("coverage"),
            "before": row.before,
            "after": row.after,
            "created_at": _j(row.created_at),
        }

    # ----------------------------------------------------------------- writes
    def update_plan(self, plan_id: str, body: dict[str, Any], *, idempotency_key: str | None, if_match: int | None) -> WriteResult:
        fields = {k: v for k, v in body.items() if v is not None}
        if not fields:
            raise ApiError(422, "NOTHING_TO_UPDATE", "Send at least one field to change.")

        def mutate(conn: Connection, row: Row) -> _Outcome:
            renewal = fields.get("renewal_date")
            if renewal is not None and date.fromisoformat(str(renewal)) <= row.effective_date:
                raise ApiError(
                    422,
                    "INVALID_DATE",
                    f"The renewal date must fall after the plan's effective date, {row.effective_date.isoformat()}.",
                    [{"field": "renewal_date", "problem": "after the effective date"}],
                )
            changed = {k: v for k, v in fields.items() if _j(getattr(row, k)) != _j(v)}
            if changed:
                values = {k: (date.fromisoformat(v) if k == "renewal_date" else v) for k, v in changed.items()}
                conn.execute(update(self.t.plans).where(self.t.plans.c.plan_id == row.plan_id).values(**values))
            return _Outcome(200, sorted(changed), {k: _j(getattr(row, k)) for k in sorted(changed)})

        return self._write("plan", plan_id, "update_plan", fields, idempotency_key, if_match, mutate)

    def update_customer_address(
        self, customer_number: str, address: AddressIn, *, idempotency_key: str | None, if_match: int | None
    ) -> WriteResult:
        """Replace the customer's mailing address on this platform: every plan
        the customer holds here is mailed to it. ``if_match`` is the
        customer's version."""
        self._check_customer_number(customer_number)
        state = address.state.upper()
        problems = []
        if state not in US_STATES:
            problems.append(("INVALID_STATE", "state", f"{address.state!r} is not a US state code"))
        if not POSTAL_CODE.match(address.postal_code):
            problems.append(("INVALID_ZIP", "postal_code", "five digits, or five plus four (43215-1234)"))
        if self.p.refuses_po_box and any(PO_BOX.search(x or "") for x in (address.line1, address.line2)):
            problems.append(("PO_BOX_NOT_ACCEPTED", "line1", f"{self.p.name} cannot deliver plan documents to a PO Box"))
        if problems:
            raise ApiError(
                422,
                problems[0][0],
                "The address was refused: " + "; ".join(f"{f}: {m}" for _, f, m in problems) + ".",
                [{"field": f, "problem": m} for _, f, m in problems],
            )
        new = {
            "address_line1": address.line1,
            "address_line2": address.line2 or None,
            "city": address.city,
            "state": state,
            "postal_code": address.postal_code,
        }

        def mutate(conn: Connection, row: Row) -> _Outcome:
            if all(getattr(row, k) == v for k, v in new.items()):
                return _Outcome(200, [], None)
            c = self.t.customers
            conn.execute(update(c).where(c.c.customer_number == row.customer_number).values(**new))
            return _Outcome(200, ["address"], {"address": self._address(row)})

        return self._write("customer", customer_number, "update_customer_address", {**new}, idempotency_key, if_match, mutate)

    def add_coverage(self, plan_id: str, body: CoverageIn, *, idempotency_key: str | None, if_match: int | None) -> WriteResult:
        cov = self.t.coverages
        code = body.coverage_code.upper()

        def mutate(conn: Connection, row: Row) -> _Outcome:
            option = self.p.option(row.product_line, code)
            if option is None:
                offered = ", ".join(o.code for o in self.p.coverage_catalog[row.product_line])
                raise ApiError(
                    422,
                    "PRODUCT_NOT_OFFERED",
                    f"{code} cannot be added to a {row.product_line} plan on {self.p.name}; it offers {offered}.",
                    [{"field": "coverage_code", "problem": f"one of {offered}"}],
                )
            if body.effective_date < row.effective_date:
                raise ApiError(
                    422,
                    "INVALID_DATE",
                    f"A coverage cannot start before its plan, which starts {row.effective_date.isoformat()}.",
                    [{"field": "effective_date", "problem": "on or after the plan's effective date"}],
                )
            terms = _check_terms(option, body.terms)
            existing = conn.execute(
                select(cov.c.coverage_id).where(
                    cov.c.plan_id == row.plan_id,
                    cov.c.coverage_code == code,
                    cov.c.employee_class == body.employee_class,
                    cov.c.status == "active",
                )
            ).scalar()
            if existing is not None:
                raise ApiError(
                    409,
                    "DUPLICATE_COVERAGE",
                    f"The plan already has an active {code} coverage for {body.employee_class} employees ({existing}); "
                    "terminate it first to replace it.",
                )
            seq = next_id(conn, self.p.schema, "coverage_seq")
            coverage_id = f"{self.p.coverage_prefix}-{seq:06d}"
            conn.execute(
                insert(cov).values(
                    coverage_id=coverage_id,
                    plan_id=row.plan_id,
                    product_line=row.product_line,
                    coverage_code=code,
                    employee_class=body.employee_class,
                    effective_date=body.effective_date,
                    terms=terms,
                )
            )
            added = conn.execute(select(cov).where(cov.c.coverage_id == coverage_id)).one()
            return _Outcome(201, ["coverages"], None, {"coverage": self._coverage_json(added)})

        request = body.model_dump(mode="json")
        request["coverage_code"] = code
        return self._write("plan", plan_id, "add_coverage", request, idempotency_key, if_match, mutate)

    def terminate_coverage(
        self, plan_id: str, coverage_id: str, body: TerminateIn, *, idempotency_key: str | None, if_match: int | None
    ) -> WriteResult:
        cov = self.t.coverages

        def mutate(conn: Connection, row: Row) -> _Outcome:
            current = conn.execute(
                select(cov).where(cov.c.coverage_id == coverage_id, cov.c.plan_id == row.plan_id).with_for_update()
            ).first()
            if current is None:
                raise ApiError(404, "COVERAGE_NOT_FOUND", f"Plan {row.plan_id} has no coverage {coverage_id}.")
            if current.status == "terminated":
                raise ApiError(
                    409,
                    "COVERAGE_ALREADY_TERMINATED",
                    f"Coverage {coverage_id} was terminated effective {current.termination_date.isoformat()}.",
                )
            if body.termination_date < current.effective_date:
                raise ApiError(
                    422,
                    "INVALID_DATE",
                    f"A coverage cannot end before it starts ({current.effective_date.isoformat()}).",
                    [{"field": "termination_date", "problem": "on or after the coverage's effective date"}],
                )
            conn.execute(
                update(cov)
                .where(cov.c.coverage_id == coverage_id)
                .values(status="terminated", termination_date=body.termination_date, termination_reason=body.reason, updated_at=func.now())
            )
            ended = conn.execute(select(cov).where(cov.c.coverage_id == coverage_id)).one()
            return _Outcome(200, ["coverages"], {"coverage": self._coverage_json(current)}, {"coverage": self._coverage_json(ended)})

        request = {"coverage_id": coverage_id, **body.model_dump(mode="json")}
        return self._write("plan", plan_id, "terminate_coverage", request, idempotency_key, if_match, mutate)

    # ---------------------------------------------------------------- helpers
    def _write(
        self,
        kind: str,
        key: str,
        operation: str,
        request: dict[str, Any],
        idempotency_key: str | None,
        if_match: int | None,
        mutate: Callable[[Connection, Row], _Outcome],
    ) -> WriteResult:
        """One write to a plan (``kind`` "plan", ``key`` its id) or to a
        customer record (``kind`` "customer", ``key`` its number). The answer
        holds the record as it now stands under ``kind``."""
        ch = self.t.changes
        table, column = (self.t.plans, self.t.plans.c.plan_id) if kind == "plan" else (self.t.customers, self.t.customers.c.customer_number)
        label = f"{kind.capitalize()} {key}"
        digest = hashlib.sha256(
            json.dumps({"operation": operation, kind: key, "request": request}, sort_keys=True, default=str).encode()
        ).hexdigest()

        def read(conn: Connection, *, lock: bool = False) -> Row:
            return self._plan_row(conn, key, lock=lock) if kind == "plan" else self._customer_row(conn, key, lock=lock)

        def as_json(conn: Connection, row: Row) -> dict[str, Any]:
            return self._plan_json(conn, row) if kind == "plan" else self._customer_json(conn, row)

        for attempt in (1, 2):
            try:
                with engine().begin() as conn:
                    row = read(conn, lock=True)
                    if idempotency_key:
                        prior = conn.execute(select(ch).where(ch.c.idempotency_key == idempotency_key)).first()
                        if prior is not None:
                            if prior.request_hash != digest:
                                raise ApiError(
                                    422,
                                    "IDEMPOTENCY_KEY_REUSED",
                                    f"Idempotency key already used for a different request ({prior.operation} on "
                                    f"{prior.plan_id or prior.customer_number}, {prior.change_id}); send a new key for a new change.",
                                )
                            return WriteResult(prior.status_code, prior.response, replay=True)
                    if if_match is not None and if_match != row.version:
                        raise ApiError(
                            412,
                            "VERSION_MISMATCH",
                            f"{label} is at version {row.version}, not {if_match}; read it again before changing it.",
                        )
                    if kind == "plan" and row.status == "terminated":
                        raise ApiError(
                            409,
                            "PLAN_TERMINATED",
                            f"Plan {key} was terminated effective {_j(row.termination_date)}; it takes no changes.",
                        )
                    if kind == "customer" and row.status != "active":
                        raise ApiError(409, "CUSTOMER_INACTIVE", f"Customer {key} is inactive on {self.p.name}; it takes no changes.")
                    before = as_json(conn, row)
                    outcome = mutate(conn, row)
                    if not outcome.changed:
                        return WriteResult(
                            200,
                            {"change_id": None, "operation": operation, "changed": [], "previous": None, kind: before, **outcome.extra},
                        )
                    conn.execute(update(table).where(column == key).values(version=table.c.version + 1, updated_at=func.now()))
                    after = as_json(conn, read(conn))
                    seq = next_id(conn, self.p.schema, "change_seq")
                    change_id = f"{self.p.change_prefix}-{seq:06d}"
                    body = {
                        "change_id": change_id,
                        "operation": operation,
                        "changed": outcome.changed,
                        "previous": outcome.previous,
                        kind: after,
                        **outcome.extra,
                    }
                    conn.execute(
                        insert(ch).values(
                            change_id=change_id,
                            customer_number=row.customer_number,
                            plan_id=key if kind == "plan" else None,
                            operation=operation,
                            idempotency_key=idempotency_key,
                            request_hash=digest,
                            before=before,
                            after=after,
                            response=body,
                            status_code=outcome.status,
                        )
                    )
                    return WriteResult(outcome.status, body)
            except IntegrityError as exc:
                # The same key committed by another record's writer between our
                # lookup and our insert: look again, and answer as a replay or
                # a reuse like any later caller would get.
                if attempt == 1 and idempotency_key and "idempotency_key" in str(exc.orig):
                    continue
                raise
        raise AssertionError("unreachable")

    def _check_customer_number(self, number: str) -> None:
        if not CUSTOMER_NUMBER.match(number):
            raise ApiError(
                422,
                "INVALID_CUSTOMER_NUMBER",
                f"{number!r} is not a customer number: eight digits starting 00 or 01, e.g. 00104217.",
                [{"field": "customer_number", "problem": "eight digits starting 00 or 01"}],
            )

    def _plan_row(self, conn: Connection, plan_id: str, *, lock: bool = False) -> Row:
        stmt = select(self.t.plans).where(self.t.plans.c.plan_id == plan_id)
        if lock:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).first()
        if row is None:
            raise ApiError(404, "PLAN_NOT_FOUND", f"No plan {plan_id} on {self.p.name}.")
        return row

    def _customer_row(self, conn: Connection, customer_number: str, *, lock: bool = False) -> Row:
        stmt = select(self.t.customers).where(self.t.customers.c.customer_number == customer_number)
        if lock:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).first()
        if row is None:
            raise ApiError(404, "CUSTOMER_NOT_FOUND", f"Customer {customer_number} has no business on {self.p.name}.")
        return row

    def _customer_json(self, conn: Connection, row: Row) -> dict[str, Any]:
        """The customer and everything it holds here: each plan with its dates
        and coverages. A plan's full terms and version are ``get_plan``'s."""
        pl, cov = self.t.plans, self.t.coverages
        plans = conn.execute(select(pl).where(pl.c.customer_number == row.customer_number).order_by(pl.c.plan_id)).all()
        coverages: dict[str, list[Row]] = {}
        if plans:
            ids = [p.plan_id for p in plans]
            for r in conn.execute(select(cov).where(cov.c.plan_id.in_(ids)).order_by(cov.c.coverage_id)).all():
                coverages.setdefault(r.plan_id, []).append(r)
        return {
            "customer_number": row.customer_number,
            "platform": self.p.code,
            "legal_name": row.legal_name,
            "status": row.status,
            "address": self._address(row),
            "version": row.version,
            "updated_at": _j(row.updated_at),
            "products": self._products(plans),
            "plans": [
                {
                    "plan_id": p.plan_id,
                    "product_line": p.product_line,
                    "plan_name": p.plan_name,
                    "status": p.status,
                    "effective_date": _j(p.effective_date),
                    "termination_date": _j(p.termination_date),
                    "renewal_date": _j(p.renewal_date),
                    "coverages": [
                        {k: v for k, v in self._coverage_json(c).items() if k != "terms"} for c in coverages.get(p.plan_id, [])
                    ],
                }
                for p in plans
            ],
        }

    @staticmethod
    def _products(plans: list[Row]) -> list[str]:
        """The product lines the customer has an active plan for here."""
        return sorted({p.product_line for p in plans if p.status == "active"})

    @staticmethod
    def _history_row(row: Row) -> dict[str, Any]:
        return {
            "change_id": row.change_id,
            "operation": row.operation,
            "changed": row.response.get("changed", []),
            "previous": row.response.get("previous"),
            "created_at": _j(row.created_at),
        }

    @staticmethod
    def _address(row: Row) -> dict[str, Any]:
        return {
            "line1": row.address_line1,
            "line2": row.address_line2,
            "city": row.city,
            "state": row.state,
            "postal_code": row.postal_code,
        }

    def _plan_fields(self, row: Row, customer_name: str) -> dict[str, Any]:
        return {
            "plan_id": row.plan_id,
            "platform": self.p.code,
            "customer_number": row.customer_number,
            "customer_name": customer_name,
            "product_line": row.product_line,
            "plan_name": row.plan_name,
            "status": row.status,
            "effective_date": _j(row.effective_date),
            "termination_date": _j(row.termination_date),
            "renewal_date": _j(row.renewal_date),
            "billing_frequency": row.billing_frequency,
            "employee_contribution_pct": _j(row.employee_contribution_pct),
            "contribution_basis": row.contribution_basis,
            "version": row.version,
            "updated_at": _j(row.updated_at),
        }

    def _coverage_json(self, row: Row) -> dict[str, Any]:
        option = self.p.option(row.product_line, row.coverage_code)
        return {
            "coverage_id": row.coverage_id,
            "coverage_code": row.coverage_code,
            "coverage_name": option.name if option else row.coverage_code,
            "employee_class": row.employee_class,
            "status": row.status,
            "effective_date": _j(row.effective_date),
            "termination_date": _j(row.termination_date),
            "termination_reason": row.termination_reason,
            "terms": row.terms,
        }

    def _plan_json(self, conn: Connection, row: Row) -> dict[str, Any]:
        c, cov = self.t.customers, self.t.coverages
        name = conn.execute(select(c.c.legal_name).where(c.c.customer_number == row.customer_number)).scalar_one()
        coverages = conn.execute(select(cov).where(cov.c.plan_id == row.plan_id).order_by(cov.c.coverage_id)).all()
        plan = self._plan_fields(row, name)
        plan["coverages"] = [self._coverage_json(r) for r in coverages]
        return plan
