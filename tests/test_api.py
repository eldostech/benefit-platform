"""Both REST APIs, against the seed: what they hold, what they refuse, and
the guarantees every write keeps."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platforms.api import create_app
from platforms.config import CUSTOMER_NUMBER, PLATFORM_A
from platforms.db import engine
from tests.conftest import plan_id

HARBOR = "00104217"  # on both platforms
LAKESHORE = "01300072"  # its LTD plan on Platform A is terminated
MEADOWBROOK = "01555123"
ADDRESS = {"line1": "1 Riverside Plaza", "line2": "Suite 900", "city": "Columbus", "state": "OH", "postal_code": "43215-2310"}
STD_TERMS = {"benefit_pct": 66, "max_weekly_benefit": 2000, "elimination_days": 14}


# --------------------------------------------------------------------- access
def test_health_is_open_and_every_other_route_needs_the_key(api_a):
    assert api_a.get("/health", headers={"X-API-Key": ""}).json()["status"] == "ok"
    refused = api_a.get("/api/v1/plans", headers={"X-API-Key": "wrong"})
    assert refused.status_code == 401 and refused.json()["code"] == "UNAUTHORIZED"
    assert refused.json()["correlation_id"] == refused.headers["X-Request-ID"]


def test_the_key_is_also_accepted_as_a_bearer_token(api_a):
    ok = api_a.get("/api/v1/plans", headers={"X-API-Key": "", "Authorization": f"Bearer {PLATFORM_A.api_key}"})
    assert ok.status_code == 200


def test_an_api_with_no_key_configured_refuses_to_start(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv(PLATFORM_A.api_key_env, "")
    with pytest.raises(RuntimeError, match=PLATFORM_A.api_key_env):
        with TestClient(create_app(PLATFORM_A)):
            pass


# ------------------------------------------------------------ what each holds
def test_platform_a_holds_only_disability_and_platform_b_only_dental_and_vision(api_a, api_b):
    a = api_a.get("/api/v1/plans", params={"limit": 100}).json()["items"]
    b = api_b.get("/api/v1/plans", params={"limit": 100}).json()["items"]
    assert {p["product_line"] for p in a} == {"STD", "LTD"}
    assert {p["product_line"] for p in b} == {"DENTAL", "VISION"}
    assert all(p["plan_id"].startswith("PA-") for p in a) and all(p["plan_id"].startswith("PB-") for p in b)


def test_every_customer_number_is_eight_digits_starting_00_or_01(api_a, api_b):
    for api in (api_a, api_b):
        for c in api.get("/api/v1/customers", params={"limit": 100}).json()["items"]:
            assert CUSTOMER_NUMBER.match(c["customer_number"]), c


def test_a_customer_on_both_platforms_has_the_same_number_and_its_own_products_on_each(api_a, api_b):
    on_a = api_a.get(f"/api/v1/customers/{HARBOR}").json()
    on_b = api_b.get(f"/api/v1/customers/{HARBOR}").json()
    assert on_a["legal_name"] == on_b["legal_name"]
    assert sorted(p["product_line"] for p in on_a["plans"]) == ["LTD", "STD"]
    assert sorted(p["product_line"] for p in on_b["plans"]) == ["DENTAL", "VISION"]


def test_a_customer_with_no_business_here_is_not_found(api_a):
    pinecrest = api_a.get("/api/v1/customers/00030561")  # vision on Platform B only
    assert pinecrest.status_code == 404 and pinecrest.json()["code"] == "CUSTOMER_NOT_FOUND"


@pytest.mark.parametrize("number", ["02000000", "0010421", "001042170", "ABCD1234"])
def test_a_malformed_customer_number_is_refused(api_a, number):
    r = api_a.get("/api/v1/plans", params={"customer_number": number})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_CUSTOMER_NUMBER"


def test_a_search_for_a_product_the_platform_does_not_hold_is_refused(api_a, api_b):
    on_a = api_a.get("/api/v1/plans", params={"product_line": "DENTAL"})
    on_b = api_b.get("/api/v1/plans", params={"product_line": "STD"})
    assert on_a.status_code == on_b.status_code == 422
    assert on_a.json()["code"] == on_b.json()["code"] == "PRODUCT_NOT_OFFERED"


def test_search_finds_plans_by_part_of_the_customer_name(api_b):
    items = api_b.get("/api/v1/plans", params={"customer_name": "lakeshore"}).json()["items"]
    assert sorted(p["product_line"] for p in items) == ["DENTAL", "VISION"]


def test_get_plan_returns_its_coverages_and_its_version_as_the_etag(api_a):
    pid = plan_id(api_a, HARBOR, "STD")
    r = api_a.get(f"/api/v1/plans/{pid}")
    plan = r.json()
    assert r.headers["ETag"] == f'"{plan["version"]}"'
    assert {(c["coverage_code"], c["employee_class"]) for c in plan["coverages"]} == {("STD_CORE", "ALL"), ("STD_BUYUP", "SALARIED")}
    assert api_a.get("/api/v1/plans/PA-STD-999999").json()["code"] == "PLAN_NOT_FOUND"


# ------------------------------------------------------------------- address
def customer_address(api, number: str = HARBOR) -> str:
    return f"/api/v1/customers/{number}/address"


def test_the_address_is_the_customers_and_no_plan_has_one(api_a, api_b):
    for api, line in ((api_a, "STD"), (api_b, "DENTAL")):
        customer = api.get(f"/api/v1/customers/{HARBOR}").json()
        assert customer["address"]["line1"] == "200 Main Street" and customer["version"] == 1
        assert "address" not in api.get(f"/api/v1/plans/{plan_id(api, HARBOR, line)}").json()
        assert all("address" not in p for p in api.get("/api/v1/plans", params={"customer_number": HARBOR}).json()["items"])


def test_an_address_change_is_one_change_to_the_customer_for_all_its_plans(api_a):
    before = api_a.get(f"/api/v1/customers/{HARBOR}").json()
    plans_before = {p["plan_id"]: api_a.get(f"/api/v1/plans/{p['plan_id']}").json()["version"] for p in before["plans"]}
    r = api_a.put(customer_address(api_a), json=ADDRESS, headers={"Idempotency-Key": "idem_addr_0001"})
    body = r.json()
    assert r.status_code == 200 and body["change_id"].startswith("A-CHG-")
    assert body["previous"]["address"]["line1"] == before["address"]["line1"]
    assert body["customer"]["address"] == ADDRESS and body["customer"]["version"] == before["version"] + 1
    assert r.headers["ETag"] == f'"{before["version"] + 1}"'
    assert sorted(p["plan_id"] for p in body["customer"]["plans"]) == sorted(plans_before)
    # The plans themselves did not change: no version bump, nothing in their history.
    for pid, version in plans_before.items():
        assert api_a.get(f"/api/v1/plans/{pid}").json()["version"] == version
        assert api_a.get(f"/api/v1/plans/{pid}/changes").json()["items"] == []
    history = api_a.get(f"/api/v1/customers/{HARBOR}/changes").json()["items"]
    assert [(h["change_id"], h["operation"]) for h in history] == [(body["change_id"], "update_customer_address")]
    change = api_a.get(f"/api/v1/customers/{HARBOR}").json()
    assert change["address"] == ADDRESS


def test_a_customer_on_both_platforms_has_an_address_on_each_changed_apart(api_a, api_b):
    api_a.put(customer_address(api_a), json=ADDRESS)
    assert api_a.get(f"/api/v1/customers/{HARBOR}").json()["address"] == ADDRESS
    assert api_b.get(f"/api/v1/customers/{HARBOR}").json()["address"]["line1"] == "200 Main Street"


def test_the_same_idempotency_key_and_request_replays_the_first_response(api_a):
    first = api_a.put(customer_address(api_a), json=ADDRESS, headers={"Idempotency-Key": "idem_addr_0002"})
    again = api_a.put(customer_address(api_a), json=ADDRESS, headers={"Idempotency-Key": "idem_addr_0002"})
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json() == first.json()
    assert api_a.get(f"/api/v1/customers/{HARBOR}").json()["version"] == first.json()["customer"]["version"]


def test_the_same_idempotency_key_with_another_request_is_refused(api_a):
    api_a.put(customer_address(api_a), json=ADDRESS, headers={"Idempotency-Key": "idem_addr_0003"})
    other = api_a.put(customer_address(api_a), json={**ADDRESS, "line1": "2 Other Street"}, headers={"Idempotency-Key": "idem_addr_0003"})
    assert other.status_code == 422 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    on_a_plan = api_a.patch(
        f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}", json={"plan_name": "Renamed"}, headers={"Idempotency-Key": "idem_addr_0003"}
    )
    assert on_a_plan.status_code == 422 and on_a_plan.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_a_stale_if_match_is_refused_and_a_current_one_accepted(api_a):
    version = api_a.get(f"/api/v1/customers/{HARBOR}").json()["version"]
    stale = api_a.put(customer_address(api_a), json=ADDRESS, headers={"If-Match": f'"{version + 5}"'})
    assert stale.status_code == 412 and stale.json()["code"] == "VERSION_MISMATCH"
    assert f"Customer {HARBOR}" in stale.json()["message"]
    current = api_a.put(customer_address(api_a), json=ADDRESS, headers={"If-Match": f'"{version}"'})
    assert current.status_code == 200


def test_the_same_address_again_changes_nothing(api_a):
    first = api_a.put(customer_address(api_a), json=ADDRESS).json()
    same = api_a.put(customer_address(api_a), json=ADDRESS).json()
    assert same["change_id"] is None and same["changed"] == []
    assert same["customer"]["version"] == first["customer"]["version"]


def test_platform_b_refuses_a_po_box_and_platform_a_accepts_one(api_a, api_b):
    po_box = {"line1": "P.O. Box 1188", "city": "Columbus", "state": "OH", "postal_code": "43216"}
    on_b = api_b.put(customer_address(api_b), json=po_box)
    assert on_b.status_code == 422 and on_b.json()["code"] == "PO_BOX_NOT_ACCEPTED"
    assert on_b.json()["fields"][0]["field"] == "line1"
    on_a = api_a.put(customer_address(api_a), json=po_box)
    assert on_a.status_code == 200


def test_granite_peak_keeps_its_legacy_po_box_on_platform_a(api_a):
    assert api_a.get("/api/v1/customers/01009874").json()["address"]["line1"] == "PO Box 4471"


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [({"state": "XX"}, "INVALID_STATE", "state"), ({"postal_code": "4321A"}, "INVALID_ZIP", "postal_code")],
)
def test_a_bad_state_or_zip_is_refused_naming_the_field(api_a, change, code, field):
    r = api_a.put(customer_address(api_a), json={**ADDRESS, **change})
    assert r.status_code == 422 and r.json()["code"] == code and r.json()["fields"][0]["field"] == field


def test_a_misspelled_field_is_refused_rather_than_ignored(api_a):
    r = api_a.put(customer_address(api_a), json={**ADDRESS, "zip": "43215"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_FAILED"
    assert any(f["field"] == "zip" for f in r.json()["fields"])


def test_an_address_for_a_customer_with_no_business_here_is_not_found(api_a):
    r = api_a.put(customer_address(api_a, "00030561"), json=ADDRESS)  # vision on Platform B only
    assert r.status_code == 404 and r.json()["code"] == "CUSTOMER_NOT_FOUND"
    assert api_a.put(customer_address(api_a, "02000000"), json=ADDRESS).json()["code"] == "INVALID_CUSTOMER_NUMBER"


def test_a_plan_has_no_address_endpoint(api_a):
    r = api_a.put(f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}/address", json=ADDRESS)
    assert r.status_code in (404, 405)


# ---------------------------------------------------------------- plan terms
def test_update_plan_changes_only_what_differs_and_says_what_it_was(api_b):
    pid = plan_id(api_b, HARBOR, "DENTAL")
    r = api_b.patch(f"/api/v1/plans/{pid}", json={"employee_contribution_pct": 40, "billing_frequency": "MONTHLY"})
    body = r.json()
    assert r.status_code == 200 and body["changed"] == ["employee_contribution_pct"]
    assert body["previous"] == {"employee_contribution_pct": 25.0} and body["plan"]["employee_contribution_pct"] == 40.0


def test_update_plan_refuses_an_empty_body_and_a_renewal_before_the_start(api_b):
    pid = plan_id(api_b, HARBOR, "DENTAL")
    assert api_b.patch(f"/api/v1/plans/{pid}", json={}).json()["code"] == "NOTHING_TO_UPDATE"
    early = api_b.patch(f"/api/v1/plans/{pid}", json={"renewal_date": "2020-01-01"})
    assert early.status_code == 422 and early.json()["code"] == "INVALID_DATE"


# ----------------------------------------------------------------- coverages
def test_a_coverage_is_added_with_its_terms_checked(api_a):
    pid = plan_id(api_a, MEADOWBROOK, "STD")  # STD_CORE for HOURLY only
    r = api_a.post(
        f"/api/v1/plans/{pid}/coverages",
        json={"coverage_code": "STD_CORE", "employee_class": "SALARIED", "effective_date": "2027-01-01", "terms": STD_TERMS},
        headers={"Idempotency-Key": "idem_cov_0001"},
    )
    body = r.json()
    assert r.status_code == 201 and body["coverage"]["coverage_id"].startswith("A-COV-")
    assert body["coverage"]["terms"] == STD_TERMS and body["coverage"]["status"] == "active"
    replay = api_a.post(
        f"/api/v1/plans/{pid}/coverages",
        json={"coverage_code": "STD_CORE", "employee_class": "SALARIED", "effective_date": "2027-01-01", "terms": STD_TERMS},
        headers={"Idempotency-Key": "idem_cov_0001"},
    )
    assert replay.status_code == 201 and replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["coverage"]["coverage_id"] == body["coverage"]["coverage_id"]


@pytest.mark.parametrize(
    "code",
    ["DENTAL_PPO_BASE", "VISION_BASE", "LTD_CORE"],  # another platform's products, and the other line on this one
)
def test_a_coverage_from_another_product_line_is_refused(api_a, code):
    r = api_a.post(
        f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}/coverages",
        json={"coverage_code": code, "effective_date": "2027-01-01", "terms": {}},
    )
    assert r.status_code == 422 and r.json()["code"] == "PRODUCT_NOT_OFFERED"


def test_platform_b_refuses_disability_coverage_on_its_dental_plans(api_b):
    r = api_b.post(
        f"/api/v1/plans/{plan_id(api_b, HARBOR, 'DENTAL')}/coverages",
        json={"coverage_code": "STD_CORE", "effective_date": "2027-01-01", "terms": STD_TERMS},
    )
    assert r.status_code == 422 and r.json()["code"] == "PRODUCT_NOT_OFFERED"


def test_terms_that_do_not_fit_the_code_are_refused_field_by_field(api_a):
    r = api_a.post(
        f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}/coverages",
        json={
            "coverage_code": "STD_CORE",
            "employee_class": "HOURLY",
            "effective_date": "2027-01-01",
            "terms": {"benefit_pct": 95, "elimination_days": 30, "colour": "blue"},
        },
    )
    assert r.status_code == 422 and r.json()["code"] == "INVALID_TERMS"
    assert {f["field"] for f in r.json()["fields"]} == {
        "terms.benefit_pct",
        "terms.elimination_days",
        "terms.max_weekly_benefit",
        "terms.colour",
    }


def test_a_duplicate_active_coverage_is_a_conflict(api_a):
    r = api_a.post(
        f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}/coverages",
        json={"coverage_code": "STD_CORE", "effective_date": "2027-01-01", "terms": STD_TERMS},
    )
    assert r.status_code == 409 and r.json()["code"] == "DUPLICATE_COVERAGE"


def test_a_coverage_cannot_start_before_its_plan(api_a):
    r = api_a.post(
        f"/api/v1/plans/{plan_id(api_a, HARBOR, 'STD')}/coverages",
        json={"coverage_code": "STD_CORE", "employee_class": "HOURLY", "effective_date": "2020-01-01", "terms": STD_TERMS},
    )
    assert r.status_code == 422 and r.json()["code"] == "INVALID_DATE"


def test_terminating_a_coverage_keeps_it_with_its_date_and_reason(api_b):
    pid = plan_id(api_b, HARBOR, "VISION")
    coverage = api_b.get(f"/api/v1/plans/{pid}").json()["coverages"][0]
    path = f"/api/v1/plans/{pid}/coverages/{coverage['coverage_id']}/terminate"
    r = api_b.post(path, json={"termination_date": "2026-12-31", "reason": "Customer moved vision to another carrier"})
    body = r.json()
    assert r.status_code == 200 and body["change_id"].startswith("B-CHG-")
    assert body["coverage"]["status"] == "terminated" and body["coverage"]["termination_date"] == "2026-12-31"
    assert body["previous"]["coverage"]["status"] == "active"
    again = api_b.post(path, json={"termination_date": "2026-12-31", "reason": "again"})
    assert again.status_code == 409 and again.json()["code"] == "COVERAGE_ALREADY_TERMINATED"


def test_a_termination_before_the_coverage_started_is_refused(api_b):
    pid = plan_id(api_b, HARBOR, "VISION")
    coverage = api_b.get(f"/api/v1/plans/{pid}").json()["coverages"][0]
    r = api_b.post(
        f"/api/v1/plans/{pid}/coverages/{coverage['coverage_id']}/terminate",
        json={"termination_date": "2019-01-01", "reason": "Backdated too far"},
    )
    assert r.status_code == 422 and r.json()["code"] == "INVALID_DATE"


def test_a_coverage_on_another_plan_is_not_found(api_b):
    dental = api_b.get(f"/api/v1/plans/{plan_id(api_b, HARBOR, 'DENTAL')}").json()["coverages"][0]["coverage_id"]
    r = api_b.post(
        f"/api/v1/plans/{plan_id(api_b, HARBOR, 'VISION')}/coverages/{dental}/terminate",
        json={"termination_date": "2026-12-31", "reason": "Wrong plan"},
    )
    assert r.status_code == 404 and r.json()["code"] == "COVERAGE_NOT_FOUND"


# --------------------------------------------------------------- plan status
def test_a_terminated_plan_takes_no_changes(api_a):
    pid = plan_id(api_a, LAKESHORE, "LTD")
    r = api_a.patch(f"/api/v1/plans/{pid}", json={"billing_frequency": "QUARTERLY"})
    assert r.status_code == 409 and r.json()["code"] == "PLAN_TERMINATED"


def test_every_write_leaves_a_change_record_newest_first(api_a):
    pid = plan_id(api_a, HARBOR, "STD")
    api_a.patch(f"/api/v1/plans/{pid}", json={"billing_frequency": "QUARTERLY"})
    api_a.patch(f"/api/v1/plans/{pid}", json={"plan_name": "Harbor Point STD (2027)"})
    history = api_a.get(f"/api/v1/plans/{pid}/changes").json()["items"]
    assert [h["operation"] for h in history] == ["update_plan", "update_plan"]
    assert history[0]["previous"] == {"plan_name": "Harbor Point Short-Term Disability"}


# ---------------------------------------------------------------------- ids
def test_each_platform_numbers_its_ids_from_its_own_sequences(api_a, api_b):
    """Seeded and written in one process, as the seed and these tests are: the
    platforms' sequences share names (plan_seq, change_seq), and must not share
    numbers."""
    first_a = api_a.get("/api/v1/plans", params={"limit": 100}).json()["items"]
    first_b = api_b.get("/api/v1/plans", params={"limit": 100}).json()["items"]
    assert min(int(p["plan_id"].rsplit("-", 1)[1]) for p in first_a) == 100001
    assert min(int(p["plan_id"].rsplit("-", 1)[1]) for p in first_b) == 100001
    a = api_a.put(f"/api/v1/customers/{HARBOR}/address", json=ADDRESS).json()
    b = api_b.put(f"/api/v1/customers/{HARBOR}/address", json=ADDRESS).json()
    assert a["change_id"] == "A-CHG-000001" and b["change_id"] == "B-CHG-000001"


# ---------------------------------------------------------------- the floor
def test_the_database_refuses_a_dental_plan_on_platform_a_even_by_hand(seeded):
    with pytest.raises(IntegrityError, match="product_line_on_this_platform"):
        with engine().begin() as conn:
            conn.execute(
                text(
                    f"insert into {PLATFORM_A.schema}.plans (plan_id, customer_number, product_line, plan_name, effective_date) "
                    "values ('PA-DEN-1', :c, 'DENTAL', 'x', '2025-01-01')"
                ),
                {"c": HARBOR},
            )
