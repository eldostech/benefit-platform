"""The customers, plans and coverages both platforms start with.

Fictional customers, built to exercise what real requests run into: a
customer on both platforms (STD and LTD here, dental and vision there), one
on a single platform, a terminated plan that refuses every change,
coverages split by employee class, coverages already terminated, and a
legacy PO Box address on Platform A (which Platform B would refuse).

A customer exists on a platform only where it holds plans there, under the
same customer number on both. The mailing address is the customer's: each
platform's customer record holds one, and the plans have none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, insert, select

from platforms.config import CUSTOMER_NUMBER, PLATFORMS, Platform
from platforms.db import create_schema, drop_schema, engine, next_id, tables

Address = tuple[str, str | None, str, str, str]

CUSTOMERS: dict[str, tuple[str, Address]] = {
    "00104217": ("Harbor Point Manufacturing, Inc.", ("200 Main Street", None, "Columbus", "OH", "43215")),
    "00118833": ("Bluestem Logistics LLC", ("4100 Prairie View Drive", "Suite 300", "Omaha", "NE", "68114")),
    "01022045": ("Cedar Ridge Health Partners", ("77 Medical Center Parkway", None, "Nashville", "TN", "37203")),
    "01077310": ("Northgate Engineering Group", ("1500 Technology Way", None, "Raleigh", "NC", "27606")),
    "00255190": ("Summit Valley School District", ("9 Schoolhouse Road", None, "Boise", "ID", "83702")),
    "01300072": ("Lakeshore Hospitality Group", ("350 Lakefront Boulevard", "Floor 12", "Chicago", "IL", "60601")),
    "00409981": ("Ironwood Community Credit Union", ("812 Ironwood Avenue", None, "Duluth", "MN", "55802")),
    "01555123": ("Meadowbrook Home & Garden Stores", ("2600 Commerce Drive", None, "Plano", "TX", "75074")),
    "00030561": ("Pinecrest Software Co.", ("18 Pinecrest Lane", "Unit 4B", "Portland", "OR", "97205")),
    "01009874": ("Granite Peak Construction", ("6200 Quarry Road", None, "Denver", "CO", "80216")),
}

#: Where a platform's customer record holds another address than the one
#: above: Granite Peak's legacy PO Box on Platform A.
LOCAL_ADDRESSES: dict[tuple[str, str], Address] = {
    ("a", "01009874"): ("PO Box 4471", None, "Denver", "CO", "80216"),
}


@dataclass(frozen=True)
class Cov:
    code: str
    terms: dict
    employee_class: str = "ALL"
    effective: date | None = None
    terminated: tuple[date, str] | None = None


@dataclass(frozen=True)
class PlanSpec:
    customer: str
    line: str
    name: str
    effective: date
    renewal: date | None
    billing: str
    contribution_pct: float
    basis: str
    coverages: tuple[Cov, ...]
    terminated: date | None = None


D = date

PLANS: dict[str, tuple[PlanSpec, ...]] = {
    "a": (
        PlanSpec("00104217", "STD", "Harbor Point Short-Term Disability", D(2023, 1, 1), D(2027, 1, 1), "MONTHLY", 0, "employer_paid", (
            Cov("STD_CORE", {"benefit_pct": 60, "max_weekly_benefit": 1500, "elimination_days": 7}),
            Cov("STD_BUYUP", {"benefit_pct": 70, "max_weekly_benefit": 2500, "elimination_days": 7}, "SALARIED"),
        )),
        PlanSpec("00104217", "LTD", "Harbor Point Long-Term Disability", D(2023, 1, 1), D(2027, 1, 1), "MONTHLY", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 60, "max_monthly_benefit": 10000, "elimination_days": 90}),
        )),
        PlanSpec("01022045", "LTD", "Cedar Ridge Long-Term Disability", D(2024, 7, 1), D(2027, 7, 1), "QUARTERLY", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 60, "max_monthly_benefit": 8000, "elimination_days": 90}),
            Cov("LTD_BUYUP", {"benefit_pct": 70, "max_monthly_benefit": 20000, "elimination_days": 90}, "EXECUTIVE"),
        )),
        PlanSpec("01077310", "STD", "Northgate Short-Term Disability", D(2022, 4, 1), D(2027, 4, 1), "MONTHLY", 50, "shared", (
            Cov("STD_CORE", {"benefit_pct": 60, "max_weekly_benefit": 1000, "elimination_days": 7}, "HOURLY"),
            Cov("STD_CORE", {"benefit_pct": 60, "max_weekly_benefit": 2000, "elimination_days": 7}, "SALARIED"),
            Cov("STD_BUYUP", {"benefit_pct": 70, "max_weekly_benefit": 3000, "elimination_days": 7}, "SALARIED",
                terminated=(D(2025, 3, 31), "Buy-up withdrawn at renewal")),
        )),
        PlanSpec("00255190", "STD", "Summit Valley Short-Term Disability", D(2021, 9, 1), D(2027, 9, 1), "ANNUAL", 100, "employee_paid", (
            Cov("STD_CORE", {"benefit_pct": 50, "max_weekly_benefit": 1000, "elimination_days": 14}),
        )),
        PlanSpec("00255190", "LTD", "Summit Valley Long-Term Disability", D(2021, 9, 1), D(2027, 9, 1), "ANNUAL", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 60, "max_monthly_benefit": 6000, "elimination_days": 180}),
        )),
        PlanSpec("01300072", "LTD", "Lakeshore Long-Term Disability", D(2020, 1, 1), None, "MONTHLY", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 60, "max_monthly_benefit": 9000, "elimination_days": 90},
                terminated=(D(2025, 12, 31), "Plan terminated at the customer's request")),
        ), terminated=D(2025, 12, 31)),
        PlanSpec("00409981", "LTD", "Ironwood Long-Term Disability", D(2025, 1, 1), D(2028, 1, 1), "MONTHLY", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 66, "max_monthly_benefit": 12000, "elimination_days": 90}),
        )),
        PlanSpec("01555123", "STD", "Meadowbrook Short-Term Disability", D(2024, 1, 1), D(2027, 1, 1), "MONTHLY", 25, "shared", (
            Cov("STD_CORE", {"benefit_pct": 55, "max_weekly_benefit": 800, "elimination_days": 7}, "HOURLY"),
        )),
        PlanSpec("01009874", "STD", "Granite Peak Short-Term Disability", D(2023, 6, 1), D(2027, 6, 1), "MONTHLY", 0, "employer_paid", (
            Cov("STD_CORE", {"benefit_pct": 60, "max_weekly_benefit": 1200, "elimination_days": 7}),
        )),
        PlanSpec("01009874", "LTD", "Granite Peak Long-Term Disability", D(2023, 6, 1), D(2027, 6, 1), "MONTHLY", 0, "employer_paid", (
            Cov("LTD_CORE", {"benefit_pct": 60, "max_monthly_benefit": 9000, "elimination_days": 90}),
        )),
    ),
    "b": (
        PlanSpec("00104217", "DENTAL", "Harbor Point Dental", D(2023, 1, 1), D(2027, 1, 1), "MONTHLY", 25, "shared", (
            Cov("DENTAL_PPO_BASE", {"tier_structure": "3_TIER", "annual_max": 1500}),
        )),
        PlanSpec("00104217", "VISION", "Harbor Point Vision", D(2023, 1, 1), D(2027, 1, 1), "MONTHLY", 100, "employee_paid", (
            Cov("VISION_BASE", {"tier_structure": "3_TIER", "frequency_months": 12}),
        )),
        PlanSpec("00118833", "DENTAL", "Bluestem Dental", D(2022, 1, 1), D(2027, 1, 1), "MONTHLY", 50, "shared", (
            Cov("DENTAL_PPO_BASE", {"tier_structure": "4_TIER", "annual_max": 1500},
                terminated=(D(2024, 12, 31), "Replaced by PPO Plus")),
            Cov("DENTAL_PPO_PLUS", {"tier_structure": "4_TIER", "annual_max": 2500}, effective=D(2025, 1, 1)),
        )),
        PlanSpec("01022045", "VISION", "Cedar Ridge Vision", D(2024, 7, 1), D(2027, 7, 1), "QUARTERLY", 100, "employee_paid", (
            Cov("VISION_PLUS", {"tier_structure": "4_TIER", "frequency_months": 12}),
        )),
        PlanSpec("00255190", "DENTAL", "Summit Valley Dental", D(2021, 9, 1), D(2027, 9, 1), "ANNUAL", 20, "shared", (
            Cov("DENTAL_PPO_BASE", {"tier_structure": "2_TIER", "annual_max": 1000}),
        )),
        PlanSpec("00255190", "VISION", "Summit Valley Vision", D(2021, 9, 1), D(2027, 9, 1), "ANNUAL", 100, "employee_paid", (
            Cov("VISION_BASE", {"tier_structure": "2_TIER", "frequency_months": 24}),
        )),
        PlanSpec("01300072", "DENTAL", "Lakeshore Dental", D(2020, 1, 1), D(2027, 1, 1), "MONTHLY", 30, "shared", (
            Cov("DENTAL_PPO_PLUS", {"tier_structure": "4_TIER", "annual_max": 3000}, "SALARIED"),
            Cov("DENTAL_PPO_BASE", {"tier_structure": "4_TIER", "annual_max": 1500}, "HOURLY"),
        )),
        PlanSpec("01300072", "VISION", "Lakeshore Vision", D(2020, 1, 1), D(2027, 1, 1), "MONTHLY", 100, "employee_paid", (
            Cov("VISION_BASE", {"tier_structure": "4_TIER", "frequency_months": 12}),
        )),
        PlanSpec("01555123", "DENTAL", "Meadowbrook Dental", D(2024, 1, 1), D(2027, 1, 1), "MONTHLY", 40, "shared", (
            Cov("DENTAL_PPO_BASE", {"tier_structure": "3_TIER", "annual_max": 1250}),
        )),
        PlanSpec("00030561", "VISION", "Pinecrest Vision", D(2025, 3, 1), D(2027, 3, 1), "MONTHLY", 0, "employer_paid", (
            Cov("VISION_PLUS", {"tier_structure": "3_TIER", "frequency_months": 12}),
        )),
    ),
}


def _check() -> None:
    """The seed obeys the rules it is meant to demonstrate, or nothing loads."""
    for number in CUSTOMERS:
        assert CUSTOMER_NUMBER.match(number), number
    for key, number in LOCAL_ADDRESSES:
        assert any(s.customer == number for s in PLANS[key]), (key, number)
    for key, specs in PLANS.items():
        p = PLATFORMS[key]
        for s in specs:
            assert s.customer in CUSTOMERS, s.customer
            assert s.line in p.product_lines, (key, s.line)
            for c in s.coverages:
                assert p.option(s.line, c.code) is not None, (key, s.line, c.code)


def seed(platform: Platform, *, reset: bool = False) -> dict[str, int]:
    _check()
    t = tables(platform)
    if reset:
        drop_schema(platform)
    create_schema(platform)
    specs = PLANS[platform.key]
    with engine().begin() as conn:
        if conn.execute(select(func.count()).select_from(t.customers)).scalar_one():
            raise SystemExit(f"{platform.schema} already has data; run `python -m platforms seed --reset` to start over")
        numbers = sorted({s.customer for s in specs})
        rows = []
        for n in numbers:
            line1, line2, city, state, postal = LOCAL_ADDRESSES.get((platform.key, n)) or CUSTOMERS[n][1]
            rows.append(
                {
                    "customer_number": n,
                    "legal_name": CUSTOMERS[n][0],
                    "address_line1": line1,
                    "address_line2": line2,
                    "city": city,
                    "state": state,
                    "postal_code": postal,
                }
            )
        conn.execute(insert(t.customers), rows)
        coverages = 0
        for s in specs:
            seq = next_id(conn, platform.schema, "plan_seq")
            plan_id = f"{platform.plan_prefix}-{platform.product_lines[s.line]}-{seq:06d}"
            conn.execute(
                insert(t.plans).values(
                    plan_id=plan_id,
                    customer_number=s.customer,
                    product_line=s.line,
                    plan_name=s.name,
                    status="terminated" if s.terminated else "active",
                    effective_date=s.effective,
                    termination_date=s.terminated,
                    renewal_date=s.renewal,
                    billing_frequency=s.billing,
                    employee_contribution_pct=s.contribution_pct,
                    contribution_basis=s.basis,
                )
            )
            for c in s.coverages:
                cseq = next_id(conn, platform.schema, "coverage_seq")
                conn.execute(
                    insert(t.coverages).values(
                        coverage_id=f"{platform.coverage_prefix}-{cseq:06d}",
                        plan_id=plan_id,
                        product_line=s.line,
                        coverage_code=c.code,
                        employee_class=c.employee_class,
                        status="terminated" if c.terminated else "active",
                        effective_date=c.effective or s.effective,
                        termination_date=c.terminated[0] if c.terminated else None,
                        termination_reason=c.terminated[1] if c.terminated else None,
                        terms=c.terms,
                    )
                )
                coverages += 1
    return {"customers": len(numbers), "plans": len(specs), "coverages": coverages}
