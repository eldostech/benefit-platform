"""Request bodies. Every model forbids unknown fields: a field a caller
misspelled is a refusal naming it, never a silent no-op."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AddressIn(_Body):
    line1: str = Field(min_length=3, max_length=120, description="Street line, e.g. 200 Main Street.")
    line2: str | None = Field(default=None, max_length=120, description="Suite, floor or unit.")
    city: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=2, max_length=2, description="Two-letter US state code, e.g. OH.")
    postal_code: str = Field(min_length=5, max_length=10, description="Five digits, or ZIP+4 (43215-1234).")


class PlanUpdateIn(_Body):
    plan_name: str | None = Field(default=None, min_length=3, max_length=120)
    renewal_date: date | None = None
    billing_frequency: Literal["MONTHLY", "QUARTERLY", "ANNUAL"] | None = None
    employee_contribution_pct: float | None = Field(default=None, ge=0, le=100)
    contribution_basis: Literal["employer_paid", "employee_paid", "shared"] | None = None


class CoverageIn(_Body):
    coverage_code: str = Field(min_length=3, max_length=24, description="A code from GET /api/v1/coverage-options.")
    employee_class: Literal["ALL", "SALARIED", "HOURLY", "EXECUTIVE"] = "ALL"
    effective_date: date
    terms: dict[str, Any] = Field(description="The coverage's terms; which are required depends on the code.")


class TerminateIn(_Body):
    termination_date: date
    reason: str = Field(min_length=3, max_length=200)
