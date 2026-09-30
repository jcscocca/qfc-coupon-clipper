#!/usr/bin/env python3
"""
QFC digital coupon auto-clipper.

Runs a REAL (visible) Chromium browser via Playwright using a persistent
profile, so:
  * You log into your QFC account yourself, once. The session is saved to
    disk and reused on later runs (no password ever touches this script).
  * Because it's a genuine browser, it gets past the site's bot protection
    that blocks plain HTTP scrapers.

It then opens the digital coupons page, scrolls to load the full list, and
clicks every "Clip" button it finds, with human-like pauses between clicks.
Coupons you've already clipped (shown as "Unclip ...") are detected and left
untouched.

Usage:
    python qfc_coupon_clipper.py            # normal run
    python qfc_coupon_clipper.py --debug    # verbose: prints what it sees
    python qfc_coupon_clipper.py --max 25   # stop after clipping 25 coupons
    python qfc_coupon_clipper.py --dry-run  # find clip buttons but don't click

First run:
    1. A browser window opens at the QFC coupons page.
    2. Sign in and pick your store if prompted.
    3. The script detects when your coupons have loaded and starts on its own.
    On later runs you'll usually already be logged in and it just proceeds.

Notes:
    * Keep this to your own personal account and a normal pace. Kroger's Terms
      of Service discourage automation; --min-delay/--max-delay keep it gentle.
    * If clip buttons aren't found, run with --debug, look at the printed button
      labels, and adjust CLIP_TEXTS / CLIPPED_TEXTS below to match.
"""

import argparse
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from relevance import (
    Candidate, Estimates, filter_excluded, load_config, matching_term,
    parse_savings, rank_candidates,
)

# ---------------------------------------------------------------------------
# Configuration you may need to tweak after the first --debug run
# ---------------------------------------------------------------------------

COUPONS_URL = "https://www.qfc.com/savings/cl/coupons/"

# Where the logged-in browser profile is stored (so login persists).
PROFILE_DIR = Path.home() / ".qfc_clipper_profile"

# Accessible-name fragments (lowercase) that identify an UN-clipped coupon's
# action button. Matched case-insensitively as substrings via looks_clippable.
# QFC's current labels are "Clip for coupon: ..."; the shorter fragments below
# are legacy fallbacks in case that wording changes.
CLIP_TEXTS = ["clip for coupon", "clip", "add coupon", "load coupon", "add to card"]

# Fragments that mean the coupon is ALREADY clipped -> skip it.
CLIPPED_TEXTS = ["clipped", "unclip", "added", "remove coupon", "you clipped"]

# On-page text meaning QFC cut us off at its observed 249-coupon account limit.
# The configured cap should normally stop first; this remains a safety guard when
# QFC's count differs from the coupons visible to the script or the cap is disabled.
# It must catch QFC's real wording ("reached the maximum number of coupons you can
# clip", "coupon limit reached") in either order, without matching ordinary
# "Clip for coupon" tiles.
_LIMIT_RE = re.compile(
    r"(?:limit|maximum).{0,20}(?:reach|clip)"
    r"|(?:reach\w*|exceed\w*).{0,40}(?:limit|maximum)",
    re.I,
)

_EMPTY_COUPONS_RE = re.compile(
    r"we(?:'|’)?re\s+not\s+finding\s+any\s+coupons\s+right\s+now",
    re.I,
)

_CLEAR_FILTER_RE = re.compile(
    r"^clear(?:\s+all)?(?:\s+selected\b.*\bfilters?)?$",
    re.I,
)

# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipResult:
    clipped: int
    exhausted: bool
    limit_hit: bool = False
    planned: int = 0
    failed: int = 0
    confirmation_blocked: bool = False
    incomplete: bool = False


def log(msg, *, debug=False, is_debug_only=False):
    if is_debug_only and not debug:
        return
    print(msg, flush=True)


def human_pause(lo, hi):
    time.sleep(random.uniform(lo, hi))


def dismiss_modal(page, debug=False):
    """Close the 'Coupon Details' (or any) dialog if it's open.

    The QFC modal ignores the Escape key, so we explicitly click its X / close
    button. Returns True if a modal was found and closed. Safe to call any time
    (no-op if no modal is open).
    """
    closed = False
    for _ in range(3):  # a click can reveal a second stacked modal; loop a few times
        try:
            dialog = page.get_by_role("dialog")
            if not dialog.count() or not dialog.first.is_visible():
                break
        except Exception:
            break

        clicked = False
        # 1) A button with an accessible name mentioning "close".
        for sel in [
            "[role='dialog'] button[aria-label*='lose']",   # Close / close
            "[role='dialog'] button[title*='lose']",
            "[role='dialog'] [aria-label*='lose'][role='button']",
        ]:
            try:
                btn = page.locator(sel)
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    clicked = True
                    break
            except Exception:
                pass

        # 2) Fallback: the first button inside the dialog (the header X).
        if not clicked:
            try:
                btn = page.locator("[role='dialog'] button")
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    clicked = True
            except Exception:
                pass

        # 3) Last resort: Escape, then click the page backdrop corner.
        if not clicked:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        human_pause(0.3, 0.6)
        closed = True
    if closed:
        log("  dismissed an open modal", debug=debug, is_debug_only=True)
    return closed


def looks_clipped(label: str) -> bool:
    label = (label or "").strip().lower()
    # These are action labels, so match the beginning. A substring match makes
    # an unclipped product such as "Clip ... no sugar added" look pre-clipped.
    return any(label.startswith(t) for t in CLIPPED_TEXTS)


def looks_clippable(label: str) -> bool:
    label = (label or "").strip().lower()
    if looks_clipped(label):
        return False
    return any(label.startswith(t) for t in CLIP_TEXTS)


def detect_logged_out(page) -> bool:
    """Best-effort check for a signed-out session: a visible 'Sign In' control.

    Used only to sharpen the warning message when no coupons are found; it is
    never the sole reason to abort (a stray footer link shouldn't stop a run).
    """
    pat = re.compile(r"sign\s*in", re.I)
    for role in ("link", "button"):
        try:
            loc = page.get_by_role(role, name=pat)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:
            pass
    return False


def warn_no_coupons(page):
    """Print the shared signed-out / no-coupons diagnostic and re-login hint.

    Distinguishes a SIGNED-OUT session from an empty/blocked page so a stale
    login is actionable instead of masquerading as a config error.
    """
    if detect_logged_out(page):
        reason = "you appear to be SIGNED OUT of QFC"
    else:
        reason = "no coupons found (page may be blocked, empty, or changed)"
    log("\n" + "*" * 64)
    log(f"WARNING: {reason}.")
    log("Re-login needed: run this script interactively (WITHOUT --no-wait-login)")
    log(f"and sign in to refresh the saved session at {PROFILE_DIR}.")
    log("*" * 64)


def _iter_button_labels(page):
    """Yield (locator, label) for every on-page button with a non-empty label.

    Shared iterator for the three coupon-button scanners (scan/collect_buttons/
    collect_candidates) so aria-label extraction stays defined in one place.
    """
    buttons = page.get_by_role("button")
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
        except Exception:
            continue
        if label:
            yield b, label


_COUPON_ACTION_PREFIX_RE = re.compile(
    r"^(?:clip(?:ped)?(?:\s+for)?(?:\s+coupon)?"
    r"|unclip(?:\s+for)?(?:\s+coupon)?"
    r"|add(?:ed)?(?:\s+coupon|\s+to\s+card)?|load(?:ed)?(?:\s+coupon)?"
    r"|remove(?:\s+coupon)?)\s*:?\s*",
    re.I,
)


def _coupon_key(label: str) -> str:
    """Return the stable portion of a coupon action label.

    QFC changes labels such as ``Clip for coupon: Save $1 ...`` to
    ``Unclip for coupon: Save $1 ...`` after a successful request. Removing that
    action prefix lets confirmation match the before and after controls.
    """
    remainder = _COUPON_ACTION_PREFIX_RE.sub("", label or "")
    return " ".join(remainder.lower().split())


def _wait_for_clip_confirmation(page, locator, original_label, *,
                                timeout=6.0, poll=0.25):
    """Wait until QFC exposes the clicked coupon in its clipped state.

    Playwright's ``click`` only confirms browser event dispatch; it says nothing
    about whether QFC accepted the request. Prefer the original control's updated
    label, then rescan once for a replacement control after a React re-render.
    """
    key = _coupon_key(original_label)
    deadline = time.monotonic() + timeout
    while True:
        try:
            # The locator was created from the old accessible name. Once QFC
            # renames it to "Unclip for coupon", waiting on that stale selector
            # for Playwright's 30-second default obscures a successful request.
            current = (locator.get_attribute("aria-label", timeout=250)
                       or locator.inner_text(timeout=250) or "").strip()
            if looks_clipped(current) and _coupon_key(current) == key:
                return True
        except Exception:
            pass
        try:
            if any(
                looks_clipped(label) and _coupon_key(label) == key
                for _, label in _iter_button_labels(page)
            ):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _visible_limit_warning(page):
    try:
        warn = page.get_by_text(_LIMIT_RE)
        return bool(warn.count() and warn.first.is_visible())
    except Exception:
        return False


def scan_coupon_buttons(page):
    """Return (n_clippable, n_clipped) over all buttons currently on the page."""
    n_clip = n_clipped = 0
    for _, label in _iter_button_labels(page):
        if looks_clipped(label):
            n_clipped += 1
        elif looks_clippable(label):
            n_clip += 1
    return n_clip, n_clipped


def detect_transient_empty_page(page) -> bool:
    """Return True for QFC's temporary empty-grid error state.

    During the live launch QFC rendered a signed-in header but left the store on
    ``Loading`` and displayed this error until the page was reloaded. This is not
    the same as being signed out or having a genuinely empty coupon inventory.
    """
    try:
        empty = page.get_by_text(_EMPTY_COUPONS_RE)
        return bool(empty.count() and empty.first.is_visible())
    except Exception:
        return False


def _reload_coupon_page(page, *, debug=False) -> bool:
    """Reload the coupon route after a transient render failure."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
        return True
    except PWTimeout:
        log("Coupon page reload timed out; continuing to wait.")
    except Exception as exc:
        log(f"Coupon page reload failed: {exc}", debug=debug,
            is_debug_only=True)
    return False


def _stable_coupon_button(page, label, fallback):
    """Resolve a coupon action by its exact accessible name at click time.

    Locators returned by ``buttons.nth(i)`` are index queries, not element
    handles. QFC re-renders the grid after filters and clips, so the same index
    can later point at a coupon image or "Shop All Items" button. An exact-name
    locator remains tied to the intended action across those re-renders.
    """
    try:
        exact = page.get_by_role("button", name=label, exact=True)
        if exact.count():
            return exact.first
    except Exception:
        pass
    return fallback


def wait_until_ready(page, *, timeout=180, poll=2.0, debug=False,
                     reload_after=10.0, max_reloads=2, prompt_login=True):
    """Poll until the coupon grid is rendered (i.e. we're signed in), instead of
    blocking on ENTER. Shows a one-time sign-in prompt if the page looks logged
    out. Returns True if coupons appeared within `timeout` seconds, else False.

    A positive signal (any clippable/clipped coupon visible) wins immediately, so
    a stray "Sign In" footer link never aborts a good session.
    """
    started = time.monotonic()
    deadline = started + timeout
    next_reload = started + reload_after
    prompted = False
    reloads = 0
    while True:
        dismiss_modal(page, debug=debug)
        n_clip, n_clipped = scan_coupon_buttons(page)
        if n_clip + n_clipped > 0:
            return True
        logged_out = detect_logged_out(page)
        if prompt_login and not prompted and logged_out:
            print("\n" + "=" * 64)
            print("Sign in to QFC in the browser window that just opened.")
            print("Clipping starts automatically once your coupons load.")
            print("=" * 64, flush=True)
            prompted = True
        now = time.monotonic()
        if (not logged_out and reloads < max_reloads and now >= next_reload
                and detect_transient_empty_page(page)):
            reloads += 1
            log(f"QFC has not populated the coupon grid; reloading "
                f"({reloads}/{max_reloads})...")
            _reload_coupon_page(page, debug=debug)
            next_reload = now + reload_after
            continue
        if now >= deadline:
            return False
        time.sleep(poll)


def collect_buttons(page, debug=False):
    """Return a list of (locator, label) for candidate buttons on the page."""
    candidates = []
    seen_labels = {}
    for b, label in _iter_button_labels(page):
        if debug:
            seen_labels[label] = seen_labels.get(label, 0) + 1
        if looks_clippable(label):
            candidates.append((_stable_coupon_button(page, label, b), label))
    if debug:
        log("  [debug] distinct button labels seen on page:", debug=debug)
        for lbl, n in sorted(seen_labels.items(), key=lambda x: -x[1])[:40]:
            log(f"     {n:>3}x  {lbl!r}", debug=debug)
    return candidates


def collect_candidates(page, estimates: Estimates, debug=False):
    """Like collect_buttons, but returns Candidate objects carrying parsed
    savings. The savings is read from the clip button's own accessible label
    (e.g. 'Clip for coupon: Save $1.50 on Daiya coupon'), which reliably carries
    the coupon's value. An earlier version parsed the enclosing tile's full text,
    but that blob contains unrelated dollar figures that swamped the real value
    (every coupon came out as the same number)."""
    candidates = []
    for b, label in _iter_button_labels(page):
        if not looks_clippable(label):
            continue
        candidates.append(
            Candidate(label=label, savings=parse_savings(label, estimates),
                      locator=_stable_coupon_button(page, label, b))
        )
    if debug:
        for c in candidates[:40]:
            log(f"  [debug] {c.savings.kind:>7} ${c.savings.value:>5.2f}"
                f"{' (est)' if c.savings.estimated else '     '}  {c.label!r}",
                debug=debug)
    return candidates


def scroll_to_load_all(page, debug=False, max_scrolls=60):
    """Scroll down repeatedly to trigger lazy-loading of all coupons."""
    last_height = 0
    stable = 0
    # Exact accessible names of real "load more" controls. We require an EXACT
    # match and reject anything mentioning a coupon/image/modal so we never
    # accidentally click a coupon tile and pop open its detail modal.
    load_more_names = ["Load more coupons", "Load More Coupons", "Load more",
                       "Show more coupons", "Show more"]
    bad_words = ("coupon modal", "view more info", "image", "info", "modal")
    for n in range(max_scrolls):
        page.mouse.wheel(0, 4000)
        human_pause(0.8, 1.6)
        height = page.evaluate("document.body.scrollHeight")
        # Try to click a genuine "Load more" control, if one exists.
        for txt in load_more_names:
            try:
                btn = page.get_by_role("button", name=txt, exact=True)
                if btn.count() and btn.first.is_visible():
                    label = (btn.first.get_attribute("aria-label")
                             or btn.first.inner_text() or "").lower()
                    if any(w in label for w in bad_words):
                        continue
                    btn.first.click()
                    log(f"  clicked '{txt}'", debug=debug, is_debug_only=True)
                    human_pause(1.0, 2.0)
                    break
            except Exception:
                pass
        # Safety: close any stray modal that may have opened.
        dismiss_modal(page, debug=debug)
        if height == last_height:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_height = height
        log(f"  scroll {n+1}: page height {height}", debug=debug, is_debug_only=True)


def _find_department_option(page, name, *, timeout=8.0, poll=0.5):
    """Poll until a visible filter option whose bare text equals `name` appears.

    The Departments panel lazy-renders its rows (its network never goes idle),
    so a single snapshot lookup races the render and can wrongly mark a present
    department as missing. Returns the option locator, or None if it never
    becomes visible within `timeout` seconds.
    """
    pat = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
    deadline = time.monotonic() + timeout
    while True:
        opts = page.get_by_text(pat)
        try:
            for i in range(opts.count()):
                o = opts.nth(i)
                if o.is_visible():
                    return o
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def _checked_filter_count(page):
    """Return the number of checked coupon facets, or None if unavailable."""
    try:
        checkboxes = page.get_by_role("checkbox")
        count = 0
        for i in range(checkboxes.count()):
            try:
                if checkboxes.nth(i).is_checked():
                    count += 1
            except Exception:
                continue
        return count
    except Exception:
        return None


def clear_filters(page, debug=False):
    """Clear every active coupon facet and verify that it is unchecked.

    QFC currently names section controls like ``Clear all selected Departments
    filters`` rather than the older ``Clear All``. Every click can also replace
    the filter DOM, so this deliberately re-resolves controls on each pass.
    """
    try:
        page.get_by_text("Departments", exact=True).first.wait_for(timeout=20000)
    except Exception:
        # The caller may be recovering a partially rendered page. Continue with
        # whatever controls are present and let inventory verification decide.
        pass

    for _ in range(100):
        checked = _checked_filter_count(page)
        if checked == 0:
            return True

        if checked is None:
            success = True
            try:
                clear_buttons = page.get_by_role("button", name=_CLEAR_FILTER_RE)
                for i in range(clear_buttons.count()):
                    try:
                        button = clear_buttons.nth(i)
                        if button.is_visible() and button.is_enabled():
                            button.click(timeout=3000)
                            human_pause(0.3, 0.6)
                    except Exception:
                        success = False
            except Exception:
                return False
            return success

        clicked = False
        try:
            clear_buttons = page.get_by_role("button", name=_CLEAR_FILTER_RE)
            for i in range(clear_buttons.count()):
                button = clear_buttons.nth(i)
                try:
                    if button.is_visible() and button.is_enabled():
                        button.click(timeout=3000)
                        clicked = True
                        # QFC exposes enabled clear buttons for inactive filter
                        # sections too. Continue through the whole set so a
                        # no-op Ways-To-Shop control cannot starve the active
                        # Departments control. Stale nodes are caught below and
                        # re-resolved on the next outer pass.
                except Exception:
                    continue
        except Exception as exc:
            log(f"  could not inspect clear-filter controls: {exc}", debug=debug,
                is_debug_only=True)

        # Fallback for a QFC variant without a clear button: toggle one checked
        # checkbox off, then re-resolve the list after the resulting re-render.
        if not clicked and checked:
            try:
                checkboxes = page.get_by_role("checkbox")
                for i in range(checkboxes.count()):
                    checkbox = checkboxes.nth(i)
                    try:
                        if (checkbox.is_checked() and checkbox.is_visible()
                                and checkbox.is_enabled()):
                            checkbox.click(timeout=3000)
                            clicked = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if not clicked:
            log("  active coupon filters remain but no clear control is usable",
                debug=debug, is_debug_only=True)
            return False
        human_pause(0.3, 0.6)

    log("  coupon filters did not settle after repeated clear attempts",
        debug=debug, is_debug_only=True)
    return False



_FILTER_READY_TIMEOUT = 20.0
_COUPON_OUTAGE_RE = re.compile(
    r"(?:intermittent\s+problems\s+with\s+digital\s+coupons"
    r"|digital\s+coupons.{0,60}(?:unavailable|experiencing\s+problems))", re.I)


def _visible_coupon_outage(page):
    try:
        notices = page.get_by_text(_COUPON_OUTAGE_RE)
        return any(notices.nth(i).is_visible() for i in range(notices.count()))
    except Exception:
        return False


def wait_for_department_filters(page, wanted, *, selected=(), timeout=None, poll=0.5):
    """Wait for complete, enabled facets; never force clicks through loading.

    One bounded deadline covers all requested departments. Polling also allows
    the browser to process filter responses before the next selection. Names in
    selected must also remain checked after a response replaces the filter DOM.
    """
    timeout = _FILTER_READY_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        if _visible_coupon_outage(page):
            log("ERROR: QFC reports digital-coupon problems; run incomplete.")
            return False
        try:
            headings = page.get_by_text("Departments", exact=True)
            checks = page.get_by_role("checkbox")
            busy = page.get_by_role("progressbar")
            ready = (headings.count() > 0 and headings.first.is_visible()
                     and checks.count() > 0
                     and not any(busy.nth(i).is_visible()
                                 for i in range(busy.count())))
            if ready:
                for i in range(checks.count()):
                    check = checks.nth(i)
                    if check.is_visible() and not check.is_enabled(timeout=1000):
                        ready = False
                        break
            if ready:
                for name in wanted:
                    options = page.get_by_role("checkbox", name=re.compile(
                        rf"^(?:CATEGORIES,\s*)?{re.escape(name)}$", re.I))
                    if not any(options.nth(i).is_visible()
                               and options.nth(i).is_enabled(timeout=1000)
                               and (name not in selected or options.nth(i).is_checked())
                               for i in range(options.count())):
                        ready = False
                        break
            if ready:
                return True
        except Exception:
            # Filters can be replaced while an asynchronous response renders.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log("ERROR: coupon filters stayed loading, disabled, incomplete, "
                "or lost requested selections; run incomplete.")
            return False
        page.wait_for_timeout(min(poll, remaining) * 1000)


def select_departments(page, wanted, debug=False):
    """Tick the requested departments in the left Departments panel.

    Use named checkbox controls and idempotent checks because filter responses
    can replace the DOM. Verify earlier selections survive each refresh, then
    verify the complete requested selection before returning success.
    Returns (matched, missing).
    """
    if not wait_for_department_filters(page, []):
        return [], list(wanted)
    if not clear_filters(page, debug=debug):
        return [], list(wanted)
    if not wait_for_department_filters(page, wanted):
        return [], list(wanted)

    matched, missing = [], []
    for name in wanted:
        selected = False
        last_error = None
        # Every filter click re-renders the panel. Re-resolve a detached row
        # instead of permanently losing that department to the render race.
        for _ in range(3):
            try:
                options = page.get_by_role("checkbox", name=re.compile(
                    rf"^(?:CATEGORIES,\s*)?{re.escape(name)}$", re.I))
                target = next((options.nth(i) for i in range(options.count())
                               if options.nth(i).is_visible()), None)
                if target is None:
                    break
                target.scroll_into_view_if_needed(timeout=3000)
                target.check(timeout=2000)
                human_pause(0.4, 0.8)
                if not wait_for_department_filters(
                        page, wanted, selected=matched + [name]):
                    return [], list(wanted)
                selected = True
                break
            except Exception as e:
                last_error = e
                if not wait_for_department_filters(page, wanted):
                    return matched, [item for item in wanted if item not in matched]
                human_pause(0.2, 0.4)
        if selected:
            matched.append(name)
        else:
            if last_error is not None:
                log(f"  could not select {name!r}: {last_error}", debug=debug,
                    is_debug_only=True)
            return matched, [item for item in wanted if item not in matched]

    human_pause(1.0, 2.0)  # let the filtered list refresh
    if not wait_for_department_filters(page, wanted, selected=wanted):
        return [], list(wanted)
    if debug:
        log(f"  [debug] departments matched={matched} missing={missing}",
            debug=debug)
    return matched, missing


# How many times to reload the list on a stalled pass before concluding the
# candidate pool is truly exhausted. >1 so a single lazy re-render doesn't cut
# the preferred phase short (and, in fill mode, wrongly clear the filters).
_STALL_RESCANS = 2


def _clip_relevant(page, cfg, budget, args, *, clicked_keys=None,
                   min_savings=None, include_nondollar=None, phase="preferred"):
    """Clip the highest-value coupons in the current list, up to `budget`.

    Re-collects + re-ranks each pass (clicking mutates the DOM). Stops on
    budget exhaustion, no progress, or a detected account-limit condition.
    Returns counts and the reason it stopped as a ClipResult.

    A click counts only after QFC changes the coupon control to its clipped state.
    Three consecutive unconfirmed clicks stop the phase, which avoids hammering a
    rate-limited or otherwise rejecting endpoint and reporting false successes.
    """
    clipped = 0
    failed = 0
    limit_hit = False
    confirmation_blocked = False
    consecutive_unconfirmed = 0
    stall_rescans = 0
    if clicked_keys is None:
        clicked_keys = set()
    if min_savings is None:
        min_savings = cfg.min_savings
    if include_nondollar is None:
        include_nondollar = cfg.include_nondollar
    wanted = cfg.departments if phase == "preferred" else []
    while clipped < budget and not limit_hit:
        if not wait_for_department_filters(page, wanted, selected=wanted):
            return ClipResult(clipped=clipped, exhausted=False, failed=failed,
                              incomplete=True)
        dismiss_modal(page, debug=args.debug)
        collected = collect_candidates(
            page, cfg.estimates, debug=(args.debug and clipped == 0))
        candidates = filter_excluded(collected, cfg.exclude_terms)
        if phase == "fill":
            # The unfiltered page is already sorted by QFC relevance/popularity.
            # Preserve that order instead of replacing it with savings ranking.
            ranked = candidates
        else:
            ranked = rank_candidates(
                candidates, min_savings=min_savings,
                include_nondollar=include_nondollar)
        if not ranked:
            if stall_rescans < _STALL_RESCANS:
                scroll_to_load_all(page, debug=args.debug)
                stall_rescans += 1
                continue
            break

        if args.dry_run:
            plan = [c for c in ranked if c.label not in clicked_keys][:budget]
            log(f"\n[dry-run] {phase} plan ({len(plan)} of {len(ranked)} "
                f"within remaining capacity {budget}):")
            for c in plan:
                est = " (est)" if c.savings.estimated else ""
                log(f"  ${c.savings.value:>6.2f}{est:<6} {c.savings.kind:<7} {c.label!r}")
                clicked_keys.add(c.label)
            excluded = [(c, matching_term(c.label, cfg.exclude_terms))
                        for c in collected]
            excluded = [(c, term) for c, term in excluded if term]
            if excluded:
                log(f"\n[dry-run] {phase} excluded by exclude_terms ({len(excluded)}):")
                for c, term in excluded:
                    log(f"  [{term}] {c.label!r}")
            return ClipResult(clipped=0, planned=len(plan),
                              exhausted=len(plan) < budget)

        progressed = False
        for c in ranked:
            if clipped >= budget:
                break
            if c.label in clicked_keys:
                continue
            try:
                if not c.locator.is_visible():
                    continue
                c.locator.scroll_into_view_if_needed(timeout=3000)
                human_pause(0.3, 0.8)
                if not wait_for_department_filters(page, wanted, selected=wanted):
                    return ClipResult(clipped=clipped, exhausted=False, failed=failed,
                                      incomplete=True)
                c.locator.click(timeout=5000)
                clicked_keys.add(c.label)
                if _visible_limit_warning(page):
                    log("Reached QFC's account clip limit; stopping.")
                    limit_hit = True
                    break
                if _wait_for_clip_confirmation(page, c.locator, c.label):
                    clipped += 1
                    progressed = True
                    consecutive_unconfirmed = 0
                    log(f"  {phase} confirmed ({clipped}/{budget}) "
                        f"${c.savings.value:.2f}: {c.label!r}")
                else:
                    failed += 1
                    consecutive_unconfirmed += 1
                    log(f"  WARNING: QFC did not confirm this clip "
                        f"({consecutive_unconfirmed}/3); not counted: {c.label!r}")
                    if consecutive_unconfirmed >= 3:
                        log("QFC stopped confirming coupon clips; stopping to avoid "
                            "false successes or further rate limiting.")
                        confirmation_blocked = True
                        break
                # Pace the next request only after observing the current result.
                # QFC may remove/re-render a successfully clipped tile quickly,
                # so sleeping before confirmation misses its short-lived state.
                human_pause(args.min_delay, args.max_delay)
            except Exception as e:
                log(f"  skip {c.label!r}: {e}", debug=args.debug, is_debug_only=True)
                dismiss_modal(page, debug=args.debug)
        if confirmation_blocked:
            break
        if not progressed:
            if stall_rescans < _STALL_RESCANS:
                scroll_to_load_all(page, debug=args.debug)
                stall_rescans += 1
                continue
            break
        stall_rescans = 0
        human_pause(1.5, 2.5)

    return ClipResult(
        clipped=clipped,
        exhausted=clipped < budget and not limit_hit and not confirmation_blocked,
        limit_hit=limit_hit,
        failed=failed,
        confirmation_blocked=confirmation_blocked,
    )


def _load_full_coupon_list(page, args):
    scroll_to_load_all(page, debug=args.debug)
    page.mouse.wheel(0, -100000)
    human_pause(1.0, 2.0)


def _load_verified_unfiltered_list(page, args, *, expected_total, attempts=3):
    """Load the unfiltered grid and verify it against a known baseline.

    The preferred-to-fill transition can briefly leave the old filtered grid in
    the DOM. A zero-candidate scan in that state is not proof of exhaustion. On
    an incomplete scan, reload the route, clear filters again, and retry.
    Returns ``(verified, n_clippable, n_clipped)`` using the best scan observed.
    """
    best = (0, 0)
    for attempt in range(attempts):
        if attempt:
            log(f"Unfiltered coupon inventory is still incomplete "
                f"({sum(best)}/{expected_total}); reloading "
                f"({attempt}/{attempts - 1})...")
            _reload_coupon_page(page, debug=args.debug)
            wait_until_ready(
                page, timeout=60, poll=2.0, debug=args.debug,
                reload_after=10.0, max_reloads=1, prompt_login=False)
            if not clear_filters(page, debug=args.debug):
                continue

        _load_full_coupon_list(page, args)
        state = scan_coupon_buttons(page)
        if sum(state) > sum(best):
            best = state
        log(f"Fill-page inventory check: {state[0]} clippable, "
            f"{state[1]} already-clipped coupon(s) visible.",
            debug=args.debug, is_debug_only=True)
        if sum(state) >= expected_total:
            return True, *state

    return False, *best


def _run_relevance_mode(page, cfg, args):
    """Clip preferred departments first, then optionally fill unused capacity."""
    if not wait_for_department_filters(page, []):
        if detect_logged_out(page):
            warn_no_coupons(page)
            return 2
        log("Stopped incomplete. Confirmed 0 coupon(s); no clips attempted.")
        return 3
    if not clear_filters(page, debug=args.debug):
        log("ERROR: could not clear persisted coupon filters; account-wide "
            "capacity cannot be calculated safely.")
        return 3

    log("Loading the unfiltered coupon list to calculate remaining capacity...")
    _load_full_coupon_list(page, args)
    n_clip, n_clipped = scan_coupon_buttons(page)
    log(f"Unfiltered page state: {n_clip} clippable, {n_clipped} already-clipped "
        "coupon(s) visible.")
    if n_clip == 0 and n_clipped == 0:
        warn_no_coupons(page)
        if getattr(args, "no_wait_login", False):
            log("Scheduled run can't proceed; exiting with status 2.")
            return 2

    already = n_clipped
    if cfg.max_clips > 0:
        account_remaining = max(0, cfg.max_clips - already)
        log(f"Remaining configured capacity: {account_remaining} "
            f"(cap {cfg.max_clips} - {already} already clipped)")
    else:
        # There is no reliable published account cap. Use the number of
        # currently clippable controls as a finite run budget and let QFC's
        # confirmed state/limit response be authoritative.
        account_remaining = n_clip
        log(f"No configured account cap; {account_remaining} coupon(s) are "
            "currently clippable.")
    run_limit = getattr(args, "max", 0)
    budget = min(account_remaining, run_limit) if run_limit else account_remaining
    if run_limit and budget < account_remaining:
        log(f"This run is limited to {budget} confirmed coupon(s) by --max.")
    if budget == 0:
        log("Already at the configured clip cap; nothing to do.")
        return 0

    matched, missing = select_departments(page, cfg.departments, debug=args.debug)
    if missing:
        log(f"ERROR: could not select every configured department: {missing}; "
            "run incomplete. Confirmed 0 coupon(s); no clips attempted.")
        return 3
    if not matched:
        log("ERROR: none of the configured departments matched the panel; "
            "aborting (set departments to valid names).")
        return 3
    log(f"Preferred departments selected: {matched}")

    log("Loading preferred coupons...")
    _load_full_coupon_list(page, args)
    if not wait_for_department_filters(
            page, cfg.departments, selected=cfg.departments):
        log("Stopped incomplete. Confirmed 0 coupon(s); no clips attempted.")
        return 3
    clicked_keys = set()
    preferred = _clip_relevant(
        page, cfg, budget, args, clicked_keys=clicked_keys,
        min_savings=cfg.min_savings,
        include_nondollar=cfg.include_nondollar,
        phase="preferred")
    preferred_used = preferred.planned if args.dry_run else preferred.clipped
    total_used = preferred_used
    limit_hit = preferred.limit_hit
    failed = preferred.failed
    confirmation_blocked = preferred.confirmation_blocked

    if preferred.incomplete:
        log(f"Stopped incomplete. Confirmed {0 if args.dry_run else total_used} "
            f"coupon(s); {failed} attempted clip(s) were not confirmed.")
        return 3

    fill_skip_reason = None
    incomplete = False
    filters_cleared_for_fill = False
    remaining = max(0, budget - total_used)
    if (cfg.fill_to_limit and remaining and not limit_hit
            and not confirmation_blocked):
        log(f"Preferred coupons exhausted with {remaining} capacity remaining; "
            "clearing filters to fill it.")
        if not clear_filters(page, debug=args.debug):
            log("WARNING: could not clear department filters; skipping the fill "
                "phase (preferred clips are kept).")
            fill_skip_reason = "department filters could not be cleared"
        else:
            filters_cleared_for_fill = True
            verified, fill_n_clip, fill_n_clipped = _load_verified_unfiltered_list(
                page, args, expected_total=n_clip + n_clipped)
            if not verified:
                log("WARNING: could not verify that QFC restored the full "
                    f"unfiltered coupon list (best scan: {fill_n_clip} clippable, "
                    f"{fill_n_clipped} already clipped; expected at least "
                    f"{n_clip + n_clipped} total). Skipping the fill phase rather "
                    "than reporting false exhaustion.")
                fill_skip_reason = "the unfiltered coupon inventory could not be verified"
            else:
                fill = _clip_relevant(
                    page, cfg, remaining, args, clicked_keys=clicked_keys,
                    min_savings=0.0, include_nondollar=True, phase="fill")
                total_used += fill.planned if args.dry_run else fill.clipped
                limit_hit = fill.limit_hit
                failed += fill.failed
                confirmation_blocked = fill.confirmation_blocked
                incomplete = fill.incomplete

    selected = [] if filters_cleared_for_fill else cfg.departments
    if incomplete or not wait_for_department_filters(
            page, cfg.departments, selected=selected):
        log(f"Stopped incomplete. Confirmed {0 if args.dry_run else total_used} "
            f"coupon(s); {failed} attempted clip(s) were not confirmed.")
        return 3

    log("\n" + "-" * 40)
    if args.dry_run:
        log(f"Dry run complete. Planned {total_used} coupon(s) against "
            f"{budget} remaining capacity.")
    elif limit_hit:
        log(f"Done. Clipped {total_used} coupon(s); QFC reported its account limit.")
    elif confirmation_blocked:
        log(f"Stopped. Confirmed {total_used} coupon(s); QFC failed to confirm "
            f"{failed} attempted clip(s). Try again later.")
    elif cfg.max_clips > 0 and total_used >= budget:
        log(f"Done. Clipped {total_used} coupon(s); configured capacity reached.")
    elif total_used >= budget:
        log(f"Done. Clipped {total_used} coupon(s); all coupons that were "
            "available at the start of the run were processed.")
    elif fill_skip_reason:
        log(f"Stopped incomplete. Confirmed {total_used} coupon(s); "
            "fill phase incomplete because "
            f"{fill_skip_reason} ({budget - total_used} capacity unused).")
    elif not cfg.fill_to_limit:
        log(f"Done. Clipped {total_used} coupon(s); preferred coupons were "
            f"exhausted with {budget - total_used} capacity remaining.")
    else:
        log(f"Done. Clipped {total_used} coupon(s); all available coupons "
            f"were exhausted with {budget - total_used} capacity remaining.")
    if failed and not confirmation_blocked:
        log(f"NOTE: {failed} attempted coupon(s) were not confirmed and were "
            "not included in the clipped total.")
    if fill_skip_reason:
        return 3
    return 4 if ((limit_hit or confirmation_blocked) and total_used == 0) else 0


def main():
    ap = argparse.ArgumentParser(description="Auto-clip QFC digital coupons.")
    ap.add_argument("--debug", action="store_true", help="verbose output")
    ap.add_argument("--dry-run", action="store_true",
                    help="find clip buttons but do not click them")
    ap.add_argument("--max", type=int, default=0,
                    help="stop after clipping this many (0 = no limit)")
    ap.add_argument("--min-delay", type=float, default=3.2,
                    help="min seconds between clips (default 3.2)")
    ap.add_argument("--max-delay", type=float, default=4.2,
                    help="max seconds between clips (default 4.2)")
    ap.add_argument("--no-wait-login", action="store_true",
                    help="skip the interactive sign-in wait (scheduled runs)")
    ap.add_argument("--config", default=None,
                    help="path to a config.toml (default: config.toml beside this script)")
    ap.add_argument("--departments", default=None,
                    help="comma-separated departments; overrides config")
    ap.add_argument("--min-savings", type=float, default=None,
                    help="skip coupons below this (estimated) dollar value")
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else (
        Path(__file__).parent / "config.toml")
    overrides = {}
    if args.departments is not None:
        overrides["departments"] = [d.strip() for d in args.departments.split(",")
                                    if d.strip()]
    if args.min_savings is not None:
        overrides["min_savings"] = args.min_savings
    cfg = load_config(config_path, overrides)
    relevance_mode = bool(cfg.departments)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,                 # visible, real browser -> passes bot checks
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        log(f"Opening {COUPONS_URL} ...")
        try:
            page.goto(COUPONS_URL, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            log("Page load timed out; continuing anyway.")

        if not args.no_wait_login:
            if not wait_until_ready(page, debug=args.debug):
                print("\nCouldn't auto-detect your coupons. If you're signed in, press "
                      "ENTER to continue; otherwise sign in first, then ENTER "
                      "(Ctrl-C to quit).")
                try:
                    input()
                except EOFError:
                    log("No interactive input; proceeding.")

        # Close any modal that may be open before we start.
        dismiss_modal(page, debug=args.debug)

        # --- relevance mode: prefer configured departments, optionally fill --
        if relevance_mode:
            rc = _run_relevance_mode(page, cfg, args)
            log("Closing in 5 seconds...")
            human_pause(5, 5)
            ctx.close()
            return rc

        log("Loading the full coupon list (scrolling)...")
        scroll_to_load_all(page, debug=args.debug)
        page.mouse.wheel(0, -100000)  # back to top
        human_pause(1.0, 2.0)

        # Surface a logged-out / blocked session clearly instead of silently
        # reporting "Clipped 0". Only treat zero-coupons as a hard stop; a stray
        # "Sign In" link while coupons exist must not abort a good run.
        n_clip, n_clipped = scan_coupon_buttons(page)
        log(f"Page state: {n_clip} clippable, {n_clipped} already-clipped "
            "coupon(s) visible.")
        if n_clip == 0 and n_clipped == 0:
            warn_no_coupons(page)
            if args.no_wait_login:
                log("Scheduled run can't proceed; exiting with status 2.")
                ctx.close()
                return 2

        clipped = 0
        failed = 0
        consecutive_unconfirmed = 0
        attempted_labels = set()
        rounds = 0
        candidates = []
        # Re-collect after each pass: clicking mutates the DOM / removes buttons.
        while True:
            rounds += 1
            # Clear any stray modal before scanning.
            dismiss_modal(page, debug=args.debug)
            candidates = collect_buttons(page, debug=args.debug and rounds == 1)
            log(f"Pass {rounds}: {len(candidates)} clippable coupon(s) found.")
            if not candidates:
                break

            progressed = False
            for b, label in candidates:
                if args.max and clipped >= args.max:
                    log(f"Reached --max {args.max}; stopping.")
                    break
                if label in attempted_labels:
                    continue
                try:
                    if not b.is_visible():
                        continue
                    b.scroll_into_view_if_needed(timeout=3000)
                    human_pause(0.3, 0.8)
                    if args.dry_run:
                        log(f"  [dry-run] would clip: {label!r}")
                    else:
                        b.click(timeout=5000)
                        attempted_labels.add(label)
                        if _visible_limit_warning(page):
                            log("Reached QFC's account clip limit; stopping.")
                            consecutive_unconfirmed = 3
                            break
                        if _wait_for_clip_confirmation(page, b, label):
                            clipped += 1
                            consecutive_unconfirmed = 0
                            log(f"  confirmed ({clipped}): {label!r}")
                            progressed = True
                        else:
                            failed += 1
                            consecutive_unconfirmed += 1
                            log(f"  WARNING: QFC did not confirm this clip "
                                f"({consecutive_unconfirmed}/3); not counted: "
                                f"{label!r}")
                            if consecutive_unconfirmed >= 3:
                                log("QFC stopped confirming coupon clips; stopping.")
                                break
                        human_pause(args.min_delay, args.max_delay)
                except Exception as e:
                    log(f"  skip {label!r}: {e}", debug=args.debug, is_debug_only=True)
                    # A modal may have popped up and blocked the click; clear it.
                    dismiss_modal(page, debug=args.debug)

            if args.dry_run:
                break
            if args.max and clipped >= args.max:
                break
            if consecutive_unconfirmed >= 3:
                break
            if not progressed:
                break
            human_pause(1.5, 2.5)

        log("\n" + "-" * 40)
        if args.dry_run:
            log(f"Dry run complete. {len(candidates)} clippable coupon(s) detected.")
        else:
            log(f"Done. Confirmed {clipped} coupon(s) across {rounds} pass(es).")
            if failed:
                log(f"{failed} attempted coupon(s) were not confirmed and were "
                    "not included in that total.")
        log("Closing in 5 seconds...")
        human_pause(5, 5)
        ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
