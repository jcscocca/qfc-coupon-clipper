"""Deterministic Chromium fixtures; no QFC network or personal profile used."""
from types import SimpleNamespace
import time

import pytest
from playwright.sync_api import sync_playwright

import qfc_coupon_clipper as clipper
from relevance import Estimates

DEPARTMENTS = [
    "Bakery", "Baking Goods", "Beverages", "Breakfast", "Canned & Packaged",
    "Condiment & Sauces", "Dairy", "Frozen", "International",
    "Natural & Organic", "Pasta", "Produce", "Snacks",
]


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(chromium, monkeypatch):
    context = chromium.new_context()
    # Any accidental outbound request is denied; tests use rendered local HTML.
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)
    original_wait = clipper.wait_for_department_filters
    monkeypatch.setattr(clipper, "wait_for_department_filters",
                        lambda page, wanted: original_wait(
                            page, wanted, timeout=0.7, poll=0.02))
    yield page
    context.close()


def filters(page, names=DEPARTMENTS, *, disabled=False, warning=False):
    checkboxes = "".join(
        f'<label><input type="checkbox" aria-label="CATEGORIES, {name}" '
        f'{"disabled" if disabled else ""}>{name}</label><br>'
        for name in names)
    notice = ('<p>We’re currently experiencing intermittent problems with '
              'Digital coupons.</p>') if warning else ''
    page.set_content('<main><h2>Departments</h2>' + notice +
        '<div role="progressbar" id="busy" hidden>Loading</div>' +
        '<div id="filters">' + checkboxes + '</div>' +
        '<button id="coupon" aria-label="Clip for coupon: Save $1.00 on Milk coupon">Clip</button>' +
        '<script>window.couponClicks=0;document.querySelector("#coupon").onclick=()=>window.couponClicks++;</script></main>')


def config():
    return SimpleNamespace(departments=DEPARTMENTS, max_clips=249,
        min_savings=0.0, include_nondollar=True, fill_to_limit=False,
        estimates=Estimates(), exclude_terms=[])


def args():
    return SimpleNamespace(dry_run=False, debug=False, min_delay=0,
        max_delay=0, max=0, no_wait_login=True)


def test_delayed_complete_filters_select_all_configured_departments(page):
    filters(page, DEPARTMENTS[:2], disabled=True)
    page.evaluate('''names => {
        document.querySelector('#busy').hidden=false;
        setTimeout(() => {
            document.querySelector('#filters').innerHTML=names.map(name =>
                `<label><input type="checkbox" aria-label="CATEGORIES, ${name}">${name}</label><br>`).join('');
            document.querySelector('#busy').hidden=true;
            document.querySelectorAll('input').forEach(input => input.onchange=()=>{
                document.querySelector('#busy').hidden=false;
                document.querySelectorAll('input').forEach(e=>e.disabled=true);
                setTimeout(()=>{
                    document.querySelectorAll('input').forEach(e=>e.disabled=false);
                    document.querySelector('#busy').hidden=true;
                },40);
            });
        },100);
    }''', DEPARTMENTS)
    matched, missing = clipper.select_departments(page, DEPARTMENTS)
    assert matched == DEPARTMENTS
    assert missing == []
    assert clipper._checked_filter_count(page) == 13
    assert page.evaluate('window.couponClicks') == 0


@pytest.mark.parametrize("condition", ["disabled", "warning", "missing"])
def test_incomplete_filters_stop_before_any_coupon_attempt(page, monkeypatch, capsys, condition):
    filters(page, DEPARTMENTS[:2] if condition == "missing" else DEPARTMENTS,
            disabled=condition == "disabled", warning=condition == "warning")
    monkeypatch.setattr(clipper, "_load_full_coupon_list", lambda page, args: None)
    monkeypatch.setattr(clipper, "_clip_relevant", lambda *a, **kw:
                        pytest.fail("Incomplete scope must never reach clipping"))
    began = time.monotonic()
    assert clipper._run_relevance_mode(page, config(), args()) == 3
    assert time.monotonic() - began < 3
    output = capsys.readouterr().out
    assert "incomplete" in output
    assert "Confirmed 0" in output
    assert "preferred coupons were exhausted" not in output
    assert page.evaluate('window.couponClicks') == 0
    if condition == "missing":
        assert "could not select every configured department" in output


def test_genuine_zero_eligible_with_ready_complete_filters_succeeds(page, monkeypatch, capsys):
    filters(page)
    loads = []
    def load(page, args):
        loads.append(1)
        if len(loads) == 2:
            page.locator('#coupon').evaluate('element => element.remove()')
    monkeypatch.setattr(clipper, "_load_full_coupon_list", load)
    monkeypatch.setattr(clipper, "scroll_to_load_all", lambda *a, **kw: None)
    assert clipper._run_relevance_mode(page, config(), args()) == 0
    assert clipper._checked_filter_count(page) == 13
    assert page.evaluate('window.couponClicks') == 0
    assert "Done. Clipped 0 coupon(s); preferred coupons were exhausted" in capsys.readouterr().out


def test_outage_after_clipping_preserves_confirmed_and_unconfirmed_counts(page, monkeypatch, capsys):
    filters(page)
    monkeypatch.setattr(clipper, "_load_full_coupon_list", lambda *a: None)
    def clipped_then_outage(*a, **kw):
        page.locator('main').evaluate('element => element.insertAdjacentHTML("afterbegin", "<p>We’re currently experiencing intermittent problems with Digital coupons.</p>")')
        return clipper.ClipResult(clipped=2, exhausted=True, failed=1)
    monkeypatch.setattr(clipper, "_clip_relevant", clipped_then_outage)
    assert clipper._run_relevance_mode(page, config(), args()) == 3
    output = capsys.readouterr().out
    assert "Confirmed 2 coupon(s); 1 attempted clip(s) were not confirmed" in output
    assert "Done. Clipped" not in output
