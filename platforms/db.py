"""The tables, one set per platform schema, and the rules the database holds
whatever the API does.

The API checks every rule first and says which was broken; the database
checks the ones that must never be broken by anything -- a script, a
migration, a psql session -- so that "Platform A holds only disability" is a
property of the data and not of one code path:

* a customer number is eight digits starting 00 or 01;
* the mailing address is the customer's: one per customer on each platform,
  with the customer's own version. A plan has no address of its own;
* a plan's product line is one this platform offers;
* a coverage carries its plan's product line (a composite foreign key) and
  its code must start with that line, so DENTAL_PPO_BASE cannot attach to an
  STD plan even by hand;
* a plan has at most one *active* coverage per code and employee class.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    Sequence,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from platforms.config import BILLING_FREQUENCIES, CONTRIBUTION_BASES, CUSTOMER_NUMBER_SQL, EMPLOYEE_CLASSES, Platform, database_url


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _stamps() -> list[Column]:
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


@dataclass(frozen=True)
class Tables:
    metadata: MetaData
    customers: Table
    plans: Table
    coverages: Table
    changes: Table
    plan_seq: Sequence
    coverage_seq: Sequence
    change_seq: Sequence


@lru_cache(maxsize=None)
def tables_for(schema: str, product_lines: tuple[str, ...]) -> Tables:
    md = MetaData(schema=schema)
    customers = Table(
        "customers",
        md,
        Column("customer_number", String(8), primary_key=True),
        Column("legal_name", String(160), nullable=False),
        Column("status", String(12), nullable=False, server_default="active"),
        Column("address_line1", String(120), nullable=False),
        Column("address_line2", String(120)),
        Column("city", String(80), nullable=False),
        Column("state", String(2), nullable=False),
        Column("postal_code", String(10), nullable=False),
        Column("version", Integer, nullable=False, server_default="1"),
        *_stamps(),
        CheckConstraint(f"customer_number ~ '{CUSTOMER_NUMBER_SQL}'", name="customer_number_format"),
        CheckConstraint("status in ('active', 'inactive')", name="customer_status"),
    )
    plans = Table(
        "plans",
        md,
        Column("plan_id", String(20), primary_key=True),
        Column("customer_number", String(8), ForeignKey(customers.c.customer_number), nullable=False, index=True),
        Column("product_line", String(10), nullable=False),
        Column("plan_name", String(120), nullable=False),
        Column("status", String(12), nullable=False, server_default="active"),
        Column("effective_date", Date, nullable=False),
        Column("termination_date", Date),
        Column("renewal_date", Date),
        Column("billing_frequency", String(10), nullable=False, server_default="MONTHLY"),
        Column("employee_contribution_pct", Numeric(5, 2), nullable=False, server_default="0"),
        Column("contribution_basis", String(16), nullable=False, server_default="employer_paid"),
        Column("version", Integer, nullable=False, server_default="1"),
        *_stamps(),
        CheckConstraint(f"product_line in ({_in(product_lines)})", name="product_line_on_this_platform"),
        CheckConstraint("status in ('active', 'terminated')", name="plan_status"),
        CheckConstraint(f"billing_frequency in ({_in(BILLING_FREQUENCIES)})", name="plan_billing_frequency"),
        CheckConstraint(f"contribution_basis in ({_in(CONTRIBUTION_BASES)})", name="plan_contribution_basis"),
        CheckConstraint("employee_contribution_pct between 0 and 100", name="plan_contribution_pct"),
        CheckConstraint("termination_date is null or termination_date >= effective_date", name="plan_dates"),
        # The target of the coverages' composite key.
        UniqueConstraint("plan_id", "product_line", name="plan_product_line"),
    )
    coverages = Table(
        "coverages",
        md,
        Column("coverage_id", String(20), primary_key=True),
        Column("plan_id", String(20), nullable=False, index=True),
        Column("product_line", String(10), nullable=False),
        Column("coverage_code", String(24), nullable=False),
        Column("employee_class", String(12), nullable=False, server_default="ALL"),
        Column("status", String(12), nullable=False, server_default="active"),
        Column("effective_date", Date, nullable=False),
        Column("termination_date", Date),
        Column("termination_reason", String(200)),
        Column("terms", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        *_stamps(),
        ForeignKeyConstraint(["plan_id", "product_line"], [plans.c.plan_id, plans.c.product_line], name="coverage_plan"),
        CheckConstraint("split_part(coverage_code, '_', 1) = product_line", name="coverage_matches_product_line"),
        CheckConstraint("status in ('active', 'terminated')", name="coverage_status"),
        CheckConstraint(f"employee_class in ({_in(EMPLOYEE_CLASSES)})", name="coverage_employee_class"),
        CheckConstraint("termination_date is null or termination_date >= effective_date", name="coverage_dates"),
        CheckConstraint("(status = 'terminated') = (termination_date is not null)", name="coverage_terminated_has_date"),
    )
    Index(
        "coverage_one_active",
        coverages.c.plan_id,
        coverages.c.coverage_code,
        coverages.c.employee_class,
        unique=True,
        postgresql_where=text("status = 'active'"),
    )
    changes = Table(
        "changes",
        md,
        Column("change_id", String(24), primary_key=True),
        # Every change is the customer's; a change to one plan also names it.
        # A change to the customer record itself (its address) names no plan.
        Column("customer_number", String(8), nullable=False, index=True),
        Column("plan_id", String(20), index=True),
        Column("operation", String(32), nullable=False),
        # Unique: one key is one change, whoever sends it twice.
        Column("idempotency_key", String(400), unique=True),
        Column("request_hash", String(64), nullable=False),
        Column("before", JSONB),
        Column("after", JSONB),
        Column("response", JSONB, nullable=False),
        Column("status_code", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    return Tables(
        metadata=md,
        customers=customers,
        plans=plans,
        coverages=coverages,
        changes=changes,
        plan_seq=Sequence("plan_seq", start=100001, metadata=md),
        coverage_seq=Sequence("coverage_seq", start=1, metadata=md),
        change_seq=Sequence("change_seq", start=1, metadata=md),
    )


def tables(platform: Platform) -> Tables:
    return tables_for(platform.schema, tuple(platform.product_lines))


def next_id(conn: Connection, schema: str, sequence: str) -> int:
    """The next value of ``<schema>.<sequence>``.

    Not ``Sequence.next_value()``: SQLAlchemy's compiled-statement cache keys
    a sequence by its name, so in a process that serves both platforms (the
    seed, the tests) Platform B's ``plan_seq`` drew from Platform A's -- both
    are named ``plan_seq`` -- and B's ids continued A's numbering. A
    statement whose text names the schema cannot be mistaken for another."""
    return conn.execute(text(f"SELECT nextval('\"{schema}\".\"{sequence}\"')")).scalar_one()


@lru_cache(maxsize=1)
def engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True, pool_size=5, max_overflow=5)


def create_schema(platform: Platform) -> None:
    """Create the platform's schema and tables if they are missing. Safe to repeat."""
    t = tables(platform)
    with engine().begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{platform.schema}"'))
        t.metadata.create_all(conn)


def drop_schema(platform: Platform) -> None:
    """Drop the platform's schema and everything in it."""
    with engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{platform.schema}" CASCADE'))
