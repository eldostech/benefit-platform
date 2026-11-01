"""The administration screens, against the seed: every form writes through
the same rules and change log as the API, a refusal is on the page with the
API's code, and a resubmitted form is not a second change."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from platforms.config import PLATFORM_A, PLATFORM_B
from platforms.web import create_web_app

HARBOR = "00104217"  # on both platforms
HARBOR_STD = "PA-STD-100001"
HARBOR_DENTAL = "PB-DEN-100001"
LAKESHORE_LTD = "PA-LTD-100007"  # terminated
ADDRESS_FORM = f"/customers/{HARBOR}/address"
ADDRESS = {"line1": "500 Oak Street", "line2": "", "city": "Columbus", "state": "OH", "postal_code": "43215"}


@pytest.fixture
def web_a(seeded):
    with TestClient(create_web_app(PLATFORM_A), follow_redirects=False) as c:
        yield c


@pytest.fixture
def web_b(seeded):
    with TestClient(create_web_app(PLATFORM_B), follow_redirects=False) as c:
        yield c


def hidden(html: str) -> dict[str, str]:
    """The form's own hidden fields: its token and the plan version it read."""
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', html))


def submit(client: TestClient, path: str, values: dict[str, str]):
    form = client.get(path)
    assert form.status_code == 200, form.text
    return client.post(path, data={**hidden(form.text), **values})


def change_id(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/changes/"), location
    return location.removeprefix("/changes/").split("?")[0]


# ----------------------------------------------------------------- reading
def test_no_sign_in_the_screens_open_on_the_plans(web_a):
    home = web_a.get("/")
    assert home.status_code == 303 and home.headers["location"] == "/plans"
    page = web_a.get("/plans")
    assert page.status_code == 200 and "no sign-in" in page.text and 'data-testid="plans-table"' in page.text


def test_plans_are_found_by_customer_number_or_name(web_a):
    by_number = web_a.get("/plans", params={"q": HARBOR}).text
    by_name = web_a.get("/plans", params={"q": "Harbor Point"}).text
    for page in (by_number, by_name):
        assert f'data-testid="plan-row-{HARBOR_STD}"' in page and 'data-testid="plan-row-PA-LTD-100002"' in page
    only_ltd = web_a.get("/plans", params={"q": HARBOR, "product_line": "LTD"}).text
    assert HARBOR_STD not in only_ltd and "PA-LTD-100002" in only_ltd


def test_each_platforms_screens_hold_only_its_own_product_lines(web_a, web_b):
    a = web_a.get("/plans", params={"q": HARBOR}).text
    b = web_b.get("/plans", params={"q": HARBOR}).text
    assert "PB-" not in a and "DENTAL" not in a
    assert "PA-" not in b and "STD" not in b
    refused = web_b.get("/plans", params={"product_line": "STD"})
    assert refused.status_code == 422 and 'data-error-code="PRODUCT_NOT_OFFERED"' in refused.text


def test_a_plan_page_shows_the_customers_address_coverages_and_history(web_a):
    page = web_a.get(f"/plans/{HARBOR_STD}").text
    assert 'data-testid="customer-address">200 Main Street, Columbus, OH 43215<' in page
    assert f'href="/customers/{HARBOR}" data-testid="open-customer"' in page
    assert 'data-testid="coverages-table"' in page and 'data-testid="add-coverage"' in page
    assert 'data-testid="edit-address"' not in page, "the address is changed on the customer"


def test_a_customer_page_shows_its_one_address_for_all_its_plans(web_a):
    page = web_a.get(f"/customers/{HARBOR}").text
    assert 'data-testid="customer-address">200 Main Street, Columbus, OH 43215<' in page
    assert 'data-testid="edit-address"' in page and f'data-testid="plan-row-{HARBOR_STD}"' in page


def test_a_missing_plan_is_a_404_page(web_a):
    page = web_a.get("/plans/PA-STD-999999")
    assert page.status_code == 404 and 'data-error-code="PLAN_NOT_FOUND"' in page.text


# ------------------------------------------------------------------ address
def test_an_address_change_on_the_screen_is_one_customer_change_like_the_apis(web_a, api_a):
    form = web_a.get(ADDRESS_FORM).text
    assert HARBOR_STD in form and "PA-LTD-100002" in form, "the form says which plans it moves"
    cid = change_id(submit(web_a, ADDRESS_FORM, ADDRESS))
    assert cid.startswith("A-CHG-")
    confirmation = web_a.get(f"/changes/{cid}?saved=1").text
    assert f'data-testid="confirmation-number">{cid}<' in confirmation and 'data-testid="saved-banner"' in confirmation
    assert 'data-testid="after-address">500 Oak Street, Columbus, OH 43215<' in confirmation
    assert f'href="/customers/{HARBOR}" data-testid="back-to-record"' in confirmation
    customer = api_a.get(f"/api/v1/customers/{HARBOR}").json()
    assert customer["address"]["line1"] == "500 Oak Street" and customer["version"] == 2
    history = api_a.get(f"/api/v1/customers/{HARBOR}/changes").json()["items"]
    assert [h["change_id"] for h in history] == [cid]
    assert api_a.get(f"/api/v1/plans/{HARBOR_STD}/changes").json()["items"] == []
    assert f'data-testid="change-row-{cid}"' in web_a.get(f"/customers/{HARBOR}").text


def test_an_old_plan_address_link_lands_on_the_customers_form(web_a):
    old = web_a.get(f"/plans/{HARBOR_STD}/address")
    assert old.status_code == 303 and old.headers["location"] == ADDRESS_FORM


def test_submitting_the_same_form_twice_is_one_change(web_a, api_a):
    form = web_a.get(ADDRESS_FORM)
    fields = {**hidden(form.text), **ADDRESS}
    first = change_id(web_a.post(ADDRESS_FORM, data=fields))
    again = change_id(web_a.post(ADDRESS_FORM, data=fields))
    assert first == again
    assert len(api_a.get(f"/api/v1/customers/{HARBOR}/changes").json()["items"]) == 1


def test_the_same_address_again_changes_nothing(web_a):
    change_id(submit(web_a, ADDRESS_FORM, ADDRESS))
    unchanged = submit(web_a, ADDRESS_FORM, ADDRESS)
    assert unchanged.status_code == 303 and unchanged.headers["location"] == f"/customers/{HARBOR}?notice=unchanged"


def test_platform_b_refuses_a_po_box_on_the_page_and_writes_nothing(web_b, api_b):
    refused = submit(web_b, ADDRESS_FORM, {**ADDRESS, "line1": "PO Box 1188"})
    assert refused.status_code == 422
    assert 'role="alert"' in refused.text and 'data-error-code="PO_BOX_NOT_ACCEPTED"' in refused.text
    assert re.search(r'id="line1"[^>]*aria-invalid="true"', refused.text)
    assert 'value="PO Box 1188"' in refused.text, "what was typed stays on the form"
    assert api_b.get(f"/api/v1/customers/{HARBOR}/changes").json()["items"] == []


def test_a_form_opened_before_someone_elses_change_is_refused(web_a):
    stale = web_a.get(ADDRESS_FORM)
    change_id(submit(web_a, ADDRESS_FORM, ADDRESS))
    refused = web_a.post(ADDRESS_FORM, data={**hidden(stale.text), **ADDRESS, "line1": "9 Elm Street"})
    assert refused.status_code == 412 and 'data-error-code="VERSION_MISMATCH"' in refused.text


def test_a_field_the_form_cannot_take_is_marked(web_a):
    refused = submit(web_a, ADDRESS_FORM, {**ADDRESS, "postal_code": "4321"})
    assert refused.status_code == 422 and 'data-testid="problem-postal_code"' in refused.text


# ------------------------------------------------------------- plan details
def test_plan_details_send_only_what_changed(web_a, api_a):
    cid = change_id(submit(web_a, f"/plans/{HARBOR_STD}/details", {"billing_frequency": "QUARTERLY"}))
    change = web_a.get(f"/changes/{cid}").text
    assert 'data-testid="after-billing_frequency">QUARTERLY<' in change
    assert api_a.get(f"/api/v1/plans/{HARBOR_STD}").json()["billing_frequency"] == "QUARTERLY"
    form = web_a.get(f"/plans/{HARBOR_STD}/details")
    unchanged = web_a.post(f"/plans/{HARBOR_STD}/details", data=hidden(form.text))
    assert unchanged.headers["location"].endswith("?notice=unchanged")


# --------------------------------------------------------------- coverages
def test_adding_a_coverage_is_choose_then_fill_its_terms(web_a, api_a):
    choose = web_a.get(f"/plans/{HARBOR_STD}/coverages/new").text
    assert 'data-testid="coverage-choose-form"' in choose and "LTD_CORE" not in choose
    terms_page = web_a.get(f"/plans/{HARBOR_STD}/coverages/new", params={"coverage_code": "STD_BUYUP"})
    assert 'id="term_benefit_pct"' in terms_page.text and 'id="term_elimination_days"' in terms_page.text
    fields = {
        **hidden(terms_page.text),
        "employee_class": "EXECUTIVE",
        "effective_date": "2027-01-01",
        "term_benefit_pct": "60",
        "term_max_weekly_benefit": "1500",
        "term_elimination_days": "7",
    }
    cid = change_id(web_a.post(f"/plans/{HARBOR_STD}/coverages/new", data=fields))
    added = [c for c in api_a.get(f"/api/v1/plans/{HARBOR_STD}").json()["coverages"] if c["employee_class"] == "EXECUTIVE"]
    assert len(added) == 1 and added[0]["terms"] == {"benefit_pct": 60, "max_weekly_benefit": 1500, "elimination_days": 7}
    assert cid.startswith("A-CHG-")


def test_a_term_out_of_range_is_marked_on_its_field(web_a):
    page = web_a.get(f"/plans/{HARBOR_STD}/coverages/new", params={"coverage_code": "STD_BUYUP"})
    fields = {
        **hidden(page.text),
        "employee_class": "HOURLY",
        "effective_date": "2027-01-01",
        "term_benefit_pct": "95",
        "term_max_weekly_benefit": "1500",
        "term_elimination_days": "7",
    }
    refused = web_a.post(f"/plans/{HARBOR_STD}/coverages/new", data=fields)
    assert refused.status_code == 422 and 'data-error-code="INVALID_TERMS"' in refused.text
    assert 'data-testid="problem-term_benefit_pct"' in refused.text


def test_terminating_a_coverage_is_reviewed_before_it_is_written(web_a, api_a):
    coverage = next(c for c in api_a.get(f"/api/v1/plans/{HARBOR_STD}").json()["coverages"] if c["status"] == "active")
    path = f"/plans/{HARBOR_STD}/coverages/{coverage['coverage_id']}/terminate"
    review = submit(web_a, path, {"termination_date": "2027-03-31", "reason": "Employer request"})
    assert review.status_code == 200 and 'data-testid="confirm"' in review.text
    assert api_a.get(f"/api/v1/plans/{HARBOR_STD}/changes").json()["items"] == [], "the review writes nothing"
    cid = change_id(web_a.post(path, data=hidden(review.text)))
    ended = next(c for c in api_a.get(f"/api/v1/plans/{HARBOR_STD}").json()["coverages"] if c["coverage_id"] == coverage["coverage_id"])
    assert ended["status"] == "terminated" and ended["termination_date"] == "2027-03-31"
    assert 'data-testid="after-coverage"' in web_a.get(f"/changes/{cid}").text


# ------------------------------------------------------------ terminated plan
def test_a_terminated_plan_offers_no_changes_and_refuses_one(web_a):
    page = web_a.get(f"/plans/{LAKESHORE_LTD}").text
    assert 'data-testid="notice-terminated"' in page and 'data-testid="edit-details"' not in page
    refused = submit(web_a, f"/plans/{LAKESHORE_LTD}/details", {"billing_frequency": "QUARTERLY"})
    assert refused.status_code == 409 and 'data-error-code="PLAN_TERMINATED"' in refused.text
