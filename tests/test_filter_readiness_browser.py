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
                        lambda page, wanted, **kwargs: original_wait(
                            page, wanted, timeout=0.7, poll=0.02, **kwargs))
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


def test_confirmation_wait_accepts_late_response_without_second_click(page):
    label = "Clip for coupon: Save $1.00 on Milk coupon"
    page.set_content('<button aria-label="' + label + '">Clip</button>')
    page.evaluate('''() => {
        window.clicks = 0;
        const button = document.querySelector('button');
        button.onclick = () => {
            window.clicks++;
            setTimeout(() => {
                button.setAttribute('aria-label', 'Unclip for coupon: Save $1.00 on Milk coupon');
                button.textContent = 'Unclip';
            }, 6500);
        };
    }''')
    button = page.get_by_role("button", name=label, exact=True)
    button.click()
    assert clipper._wait_for_clip_confirmation(page, button, label)
    assert page.evaluate("window.clicks") == 1


def test_replaced_filter_dom_preserves_complete_selection(page):
    filters(page)
    page.evaluate('''() => {
        document.querySelector('#filters').onchange=() => {
            document.querySelector('#busy').hidden=false;
            document.querySelectorAll('input').forEach(input=>input.disabled=true);
            setTimeout(()=>{
                const panel=document.querySelector('#filters');
                panel.replaceChildren(...Array.from(panel.children, child=>child.cloneNode(true)));
                document.querySelectorAll('input').forEach(input=>input.disabled=false);
                document.querySelector('#busy').hidden=true;
            },40);
        };
    }''')
    assert clipper.select_departments(page, DEPARTMENTS) == (DEPARTMENTS, [])
    assert page.get_by_role('checkbox', checked=True).count() == 13


def test_refresh_losing_prior_selections_stops_before_clipping(page, monkeypatch, capsys):
    filters(page)
    page.evaluate('''() => {
        document.querySelector('#filters').onchange=event=>{
            document.querySelectorAll('input').forEach(input=>{
                if (input!==event.target) input.checked=false;
            });
        };
    }''')
    monkeypatch.setattr(clipper, '_load_full_coupon_list', lambda *a: None)
    assert clipper._run_relevance_mode(page, config(), args()) == 3
    assert page.evaluate('window.couponClicks') == 0
    assert 'could not select every configured department' in capsys.readouterr().out


def test_selection_lost_during_grid_load_stops_before_clipping(page, monkeypatch):
    filters(page)
    loads = []
    def load(*a):
        loads.append(1)
        if len(loads) == 2:
            page.get_by_role('checkbox').first.evaluate('input=>input.checked=false')
    monkeypatch.setattr(clipper, '_load_full_coupon_list', load)
    assert clipper._run_relevance_mode(page, config(), args()) == 3
    assert page.evaluate('window.couponClicks') == 0


@pytest.mark.parametrize('phase,condition', [
    ('preferred', 'outage'), ('preferred', 'disabled'),
    ('preferred', 'selection'), ('fill', 'outage'), ('fill', 'disabled'),
])
@pytest.mark.parametrize('confirmed', [True, False])
def test_mid_phase_failure_stops_further_attempts(page, monkeypatch, phase, condition, confirmed):
    filters(page)
    if phase == 'preferred':
        assert clipper.select_departments(page, DEPARTMENTS) == (DEPARTMENTS, [])
    page.evaluate('''({condition,confirmed})=>{
        const first=document.querySelector('#coupon');
        first.insertAdjacentHTML('afterend', '<button aria-label="Clip for coupon: Save $2.00 on Bread coupon">Clip</button>');
        document.querySelectorAll('button').forEach(button=>button.onclick=()=>{
            window.couponClicks++;
            if (confirmed) button.setAttribute('aria-label',button.getAttribute('aria-label').replace(/^Clip /,'Unclip '));
            if (condition==='outage') document.querySelector('main').insertAdjacentHTML('afterbegin','<p>We’re currently experiencing intermittent problems with Digital coupons.</p>');
            if (condition==='disabled') document.querySelectorAll('input').forEach(input=>input.disabled=true);
            if (condition==='selection') document.querySelector('input').checked=false;
        });
    }''', {'condition': condition, 'confirmed': confirmed})
    original = clipper._wait_for_clip_confirmation
    monkeypatch.setattr(clipper, '_wait_for_clip_confirmation',
                        lambda *a: original(*a, timeout=0))
    result = clipper._clip_relevant(page, config(), 3, args(), phase=phase)
    assert page.evaluate('window.couponClicks') == 1
    assert result.incomplete is True
    assert result.exhausted is False
    assert result.clipped == int(confirmed)
    assert result.failed == int(not confirmed)


def test_mid_phase_outage_preserves_mixed_counts_and_skips_fill(page, monkeypatch, capsys):
    filters(page)
    page.evaluate('''() => {
        document.querySelector('#coupon').insertAdjacentHTML('afterend',
            '<button aria-label="Clip for coupon: Save $2.00 on Bread coupon">Clip</button><button aria-label="Clip for coupon: Save $3.00 on Eggs coupon">Clip</button>');
        document.querySelectorAll('button').forEach(button=>button.onclick=()=>{
            window.couponClicks++;
            if (window.couponClicks===1) button.setAttribute('aria-label',button.getAttribute('aria-label').replace(/^Clip /,'Unclip '));
            if (window.couponClicks===2) document.querySelector('main').insertAdjacentHTML('afterbegin','<p>We’re currently experiencing intermittent problems with Digital coupons.</p>');
        });
    }''')
    original = clipper._wait_for_clip_confirmation
    monkeypatch.setattr(clipper, '_wait_for_clip_confirmation',
                        lambda *a: original(*a, timeout=0))
    monkeypatch.setattr(clipper, '_load_full_coupon_list', lambda *a: None)
    monkeypatch.setattr(clipper, '_load_verified_unfiltered_list', lambda *a, **kw:
                        pytest.fail('Incomplete preferred phase must not enter fill'))
    cfg = config()
    cfg.fill_to_limit = True
    assert clipper._run_relevance_mode(page, cfg, args()) == 3
    assert page.evaluate('window.couponClicks') == 2
    assert 'Confirmed 1 coupon(s); 1 attempted clip(s) were not confirmed' in capsys.readouterr().out


@pytest.mark.parametrize('cap', [0, 249])
def test_unverified_fill_returns_incomplete_with_counts(page, monkeypatch, capsys, cap):
    filters(page)
    cfg = config()
    cfg.max_clips = cap
    cfg.fill_to_limit = True
    monkeypatch.setattr(clipper, '_load_full_coupon_list', lambda *a: None)
    monkeypatch.setattr(clipper, 'scan_coupon_buttons', lambda *a: (10, 49))
    monkeypatch.setattr(clipper, '_clip_relevant', lambda *a, **kw:
                        clipper.ClipResult(clipped=2, exhausted=True, failed=1))
    monkeypatch.setattr(clipper, '_load_verified_unfiltered_list', lambda *a, **kw: (False, 0, 0))
    assert clipper._run_relevance_mode(page, cfg, args()) == 3
    output = capsys.readouterr().out
    assert 'Stopped incomplete. Confirmed 2 coupon(s); fill phase incomplete' in output
    assert '1 attempted coupon(s) were not confirmed' in output
