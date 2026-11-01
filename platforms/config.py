"""What makes Platform A Platform A: its schema, the product lines it may
hold, its coverage catalogue, its id prefixes and its ports.

The schemas are fixed here rather than configured. Two platforms writing to
one schema by a typo in an environment file would be the crisscross this
whole package exists to rule out; a constant cannot drift. The one override,
``PLATFORMS_SCHEMA_SUFFIX``, exists for the test suite alone, so tests never
touch the seeded data you test the Workbench against.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

#: A customer number: eight digits, starting 00 or 01.
CUSTOMER_NUMBER = re.compile(r"^0[01][0-9]{6}$")
CUSTOMER_NUMBER_SQL = "^0[01][0-9]{6}$"

EMPLOYEE_CLASSES = ("ALL", "SALARIED", "HOURLY", "EXECUTIVE")
BILLING_FREQUENCIES = ("MONTHLY", "QUARTERLY", "ANNUAL")
CONTRIBUTION_BASES = ("employer_paid", "employee_paid", "shared")


@dataclass(frozen=True)
class CoverageOption:
    """One coverage a plan of a product line can carry, and the terms it needs."""

    code: str
    name: str
    #: term name -> (kind, allowed): kind is "int_range", "num_range" or "choice".
    terms: dict[str, tuple[str, tuple]] = field(default_factory=dict)


_DISABILITY_STD = (
    CoverageOption(
        "STD_CORE",
        "Short-term disability, employer core",
        {"benefit_pct": ("int_range", (40, 80)), "max_weekly_benefit": ("num_range", (100, 5000)), "elimination_days": ("choice", (0, 7, 14))},
    ),
    CoverageOption(
        "STD_BUYUP",
        "Short-term disability, voluntary buy-up",
        {"benefit_pct": ("int_range", (40, 80)), "max_weekly_benefit": ("num_range", (100, 5000)), "elimination_days": ("choice", (0, 7, 14))},
    ),
)
_DISABILITY_LTD = (
    CoverageOption(
        "LTD_CORE",
        "Long-term disability, employer core",
        {"benefit_pct": ("int_range", (40, 80)), "max_monthly_benefit": ("num_range", (500, 25000)), "elimination_days": ("choice", (90, 180))},
    ),
    CoverageOption(
        "LTD_BUYUP",
        "Long-term disability, voluntary buy-up",
        {"benefit_pct": ("int_range", (40, 80)), "max_monthly_benefit": ("num_range", (500, 25000)), "elimination_days": ("choice", (90, 180))},
    ),
)
_DENTAL = (
    CoverageOption(
        "DENTAL_PPO_BASE",
        "Dental PPO, base",
        {"tier_structure": ("choice", ("2_TIER", "3_TIER", "4_TIER")), "annual_max": ("num_range", (500, 5000))},
    ),
    CoverageOption(
        "DENTAL_PPO_PLUS",
        "Dental PPO, enhanced",
        {"tier_structure": ("choice", ("2_TIER", "3_TIER", "4_TIER")), "annual_max": ("num_range", (500, 5000))},
    ),
)
_VISION = (
    CoverageOption(
        "VISION_BASE",
        "Vision, base",
        {"tier_structure": ("choice", ("2_TIER", "3_TIER", "4_TIER")), "frequency_months": ("choice", (12, 24))},
    ),
    CoverageOption(
        "VISION_PLUS",
        "Vision, enhanced",
        {"tier_structure": ("choice", ("2_TIER", "3_TIER", "4_TIER")), "frequency_months": ("choice", (12, 24))},
    ),
)


@dataclass(frozen=True)
class Platform:
    key: str
    code: str
    name: str
    #: The PostgreSQL schema, fixed. See the module docstring.
    base_schema: str
    #: product line -> the short form used in plan ids.
    product_lines: dict[str, str]
    coverage_catalog: dict[str, tuple[CoverageOption, ...]]
    plan_prefix: str
    change_prefix: str
    coverage_prefix: str
    api_port: int
    mcp_port: int
    api_key_env: str
    #: The administration screens (`platforms.web`): what a person -- or a
    #: browser agent, for a platform that takes no API calls -- uses. Test
    #: screens with no sign-in.
    web_port: int = 0
    #: How the screens present themselves: a product name and an accent.
    web_title: str = ""
    web_accent: str = "#1f5fa8"
    #: Platform B's mail room cannot deliver plan documents to a PO Box, so
    #: its API refuses one -- a realistic business refusal for testing an
    #: escalation. Platform A takes them.
    refuses_po_box: bool = False

    @property
    def schema(self) -> str:
        return self.base_schema + os.environ.get("PLATFORMS_SCHEMA_SUFFIX", "")

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def api_base_url(self) -> str:
        return os.environ.get(f"{self.api_key_env.removesuffix('_API_KEY')}_API_URL", f"http://127.0.0.1:{self.api_port}")

    def option(self, product_line: str, code: str) -> CoverageOption | None:
        return next((o for o in self.coverage_catalog.get(product_line, ()) if o.code == code), None)


PLATFORM_A = Platform(
    key="a",
    code="platform-a",
    name="Platform A (disability)",
    base_schema="platform_a",
    product_lines={"STD": "STD", "LTD": "LTD"},
    coverage_catalog={"STD": _DISABILITY_STD, "LTD": _DISABILITY_LTD},
    plan_prefix="PA",
    change_prefix="A-CHG",
    coverage_prefix="A-COV",
    api_port=8201,
    mcp_port=8211,
    api_key_env="PLATFORM_A_API_KEY",
    web_port=8221,
    web_title="Platform A · Disability Administration",
    web_accent="#1f5fa8",
)

PLATFORM_B = Platform(
    key="b",
    code="platform-b",
    name="Platform B (dental and vision)",
    base_schema="platform_b",
    product_lines={"DENTAL": "DEN", "VISION": "VIS"},
    coverage_catalog={"DENTAL": _DENTAL, "VISION": _VISION},
    plan_prefix="PB",
    change_prefix="B-CHG",
    coverage_prefix="B-COV",
    api_port=8202,
    mcp_port=8212,
    api_key_env="PLATFORM_B_API_KEY",
    web_port=8222,
    web_title="Platform B · Dental & Vision Administration",
    web_accent="#1d7a5a",
    refuses_po_box=True,
)

PLATFORMS = {"a": PLATFORM_A, "b": PLATFORM_B}


@dataclass(frozen=True)
class Directory:
    """The enterprise plan directory: one API over every admin system, saying
    which system hosts each of a customer's plans. It holds no data; every
    answer is the platforms' own, asked live."""

    code: str = "plan-directory"
    name: str = "Enterprise plan directory"
    api_port: int = 8203
    mcp_port: int = 8213
    api_key_env: str = "PLAN_DIRECTORY_API_KEY"
    #: The systems it federates, in the order it lists them.
    sources: tuple[Platform, ...] = (PLATFORM_A, PLATFORM_B)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def api_base_url(self) -> str:
        return os.environ.get("PLAN_DIRECTORY_API_URL", f"http://127.0.0.1:{self.api_port}")


DIRECTORY = Directory()


def platform(key: str) -> Platform:
    try:
        return PLATFORMS[key.lower().removeprefix("platform-").removeprefix("platform_")]
    except KeyError:
        raise SystemExit(f"unknown platform {key!r}; use a or b") from None


def database_url() -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD") or None,
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ.get("DB_NAME", "postgres"),
    ).render_as_string(hide_password=False)
