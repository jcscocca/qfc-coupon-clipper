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
    3. Return to the terminal and press ENTER. The script takes over and clips.
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
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from relevance import (
    Candidate, Estimates, load_config, parse_savings, rank_candidates,
)

# ---------------------------------------------------------------------------
# Configuration you may need to tweak after the first --debug run
# ---------------------------------------------------------------------------

COUPONS_URL = "https://www.qfc.com/savings/cl/coupons/"

# Where the logged-in browser profile is stored (so login persists).
PROFILE_DIR = Path.home() / ".qfc_clipper_profile"

# Accessible-name fragments (lowercase) that identify an UN-clipped coupon's
# action button. Matched case-insensitively as substrings.
# NOTE: QFC labels these "Clip for coupon: ...". We also require the word
# "coupon" to be present (see looks_clippable) so we only match coupon tiles.
CLIP_TEXTS = ["clip for coupon", "clip", "add coupon", "load coupon", "add to card"]

# Fragments that mean the coupon is ALREADY clipped -> skip it.
CLIPPED_TEXTS = ["clipped", "unclip", "added", "remove coupon"]

# ---------------------------------------------------------------------------


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
    label = (label or "").lower()
    return any(t in label for t in CLIPPED_TEXTS)


def looks_clippable(label: str) -> bool:
    label = (label or "").lower()
    if looks_clipped(label):
        return False
    return any(t in label for t in CLIP_TEXTS)


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


def scan_coupon_buttons(page):
    """Return (n_clippable, n_clipped) over all buttons currently on the page."""
    n_clip = n_clipped = 0
    buttons = page.get_by_role("button")
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
        except Exception:
            continue
        if not label:
            continue
        if looks_clipped(label):
            n_clipped += 1
        elif looks_clippable(label):
            n_clip += 1
    return n_clip, n_clipped


def wait_until_ready(page, *, timeout=180, poll=2.0, debug=False):
    """Poll until the coupon grid is rendered (i.e. we're signed in), instead of
    blocking on ENTER. Shows a one-time sign-in prompt if the page looks logged
    out. Returns True if coupons appeared within `timeout` seconds, else False.

    A positive signal (any clippable/clipped coupon visible) wins immediately, so
    a stray "Sign In" footer link never aborts a good session.
    """
    deadline = time.monotonic() + timeout
    prompted = False
    while True:
        dismiss_modal(page, debug=debug)
        n_clip, n_clipped = scan_coupon_buttons(page)
        if n_clip + n_clipped > 0:
            return True
        if not prompted and detect_logged_out(page):
            print("\n" + "=" * 64)
            print("Sign in to QFC in the browser window that just opened.")
            print("Clipping starts automatically once your coupons load.")
            print("=" * 64, flush=True)
            prompted = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def count_clipped_total(page, debug=False):
    """Best-effort count of coupons already clipped on the account.

    v1: reuse scan_coupon_buttons on the CURRENT view (the count of 'Unclip'
    style buttons). NOTE: when a department filter is active this is the
    filtered count, not account-wide; the runtime limit safety net (Task 9)
    is what actually guarantees we never exceed the cap. Confirm via --debug
    whether QFC exposes an account-wide clipped count to improve this later.
    """
    try:
        _, n_clipped = scan_coupon_buttons(page)
        if debug:
            log(f"  [debug] already-clipped (current view): {n_clipped}",
                debug=debug)
        return n_clipped
    except Exception:
        return 0


def collect_buttons(page, debug=False):
    """Return a list of (locator, label) for candidate buttons on the page."""
    candidates = []
    buttons = page.get_by_role("button")
    count = buttons.count()
    seen_labels = {}
    for i in range(count):
        b = buttons.nth(i)
        try:
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
        except Exception:
            continue
        if not label:
            continue
        if debug:
            seen_labels[label] = seen_labels.get(label, 0) + 1
        if looks_clippable(label):
            candidates.append((b, label))
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
    buttons = page.get_by_role("button")
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
        except Exception:
            continue
        if not label or not looks_clippable(label):
            continue
        candidates.append(
            Candidate(label=label, savings=parse_savings(label, estimates),
                      locator=b)
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


def select_departments(page, wanted, debug=False):
    """Tick the requested departments in the left Departments panel.

    The coupons page is a single-page app that renders the filter panel after
    load (its network never goes idle), so we first wait for the panel. Each
    department is a <label>-wrapped checkbox whose only bare visible text is the
    department name — coupon cards carry longer descriptions — so we target each
    by an exact, case-insensitive text match and click it (clicking the label's
    text toggles the checkbox). Persisted selections are cleared first via the
    facets' "Clear All" buttons, so from the reset state each click checks it.
    Returns (matched, missing).
    """
    # Wait for the filter panel to render.
    try:
        page.get_by_text("Departments", exact=True).first.wait_for(timeout=20000)
    except Exception:
        log("  Departments panel did not appear in time", debug=debug,
            is_debug_only=True)

    # Reset any previously-applied filters (one "Clear All" per facet section).
    clear_all = page.get_by_role("button", name="Clear All")
    for i in range(clear_all.count()):
        try:
            btn = clear_all.nth(i)
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                human_pause(0.3, 0.6)
        except Exception:
            pass

    matched, missing = [], []
    for name in wanted:
        pat = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
        try:
            opts = page.get_by_text(pat)
            target = None
            for i in range(opts.count()):
                if opts.nth(i).is_visible():
                    target = opts.nth(i)
                    break
            if target is None:
                missing.append(name)
                continue
            target.scroll_into_view_if_needed(timeout=3000)
            target.click()
            human_pause(0.4, 0.8)
            matched.append(name)
        except Exception as e:
            log(f"  could not select {name!r}: {e}", debug=debug, is_debug_only=True)
            missing.append(name)

    human_pause(1.0, 2.0)  # let the filtered list refresh
    if debug:
        log(f"  [debug] departments matched={matched} missing={missing}",
            debug=debug)
    return matched, missing


def _clip_relevant(page, cfg, budget, args):
    """Clip the highest-value coupons in the filtered list, up to `budget`.

    Re-collects + re-ranks each pass (clicking mutates the DOM). Stops on
    budget exhaustion, no progress, or a detected account-limit condition.
    Returns an exit code (0 ok, 4 = hit account limit early).

    Limit handling: stops if QFC shows a visible "limit/maximum reached" message.
    A click that silently no-ops (no such message) is not separately detected, but
    the account cap is still respected — an ineffective click changes nothing on the
    account and the no-progress guard ends the run. (Spec §9's separate "no
    transition" detection is deferred.)
    """
    clipped = 0
    limit_hit = False
    while clipped < budget and not limit_hit:
        dismiss_modal(page, debug=args.debug)
        ranked = rank_candidates(
            collect_candidates(page, cfg.estimates, debug=(args.debug and clipped == 0)),
            min_savings=cfg.min_savings, include_nondollar=cfg.include_nondollar)
        if not ranked:
            break

        if args.dry_run:
            log(f"\n[dry-run] plan ({min(len(ranked), budget)} of {len(ranked)} "
                f"within budget {budget}):")
            for c in ranked[:budget]:
                est = " (est)" if c.savings.estimated else ""
                log(f"  ${c.savings.value:>6.2f}{est:<6} {c.savings.kind:<7} {c.label!r}")
            return 0

        progressed = False
        for c in ranked:
            if clipped >= budget:
                break
            try:
                if not c.locator.is_visible():
                    continue
                c.locator.scroll_into_view_if_needed(timeout=3000)
                human_pause(0.3, 0.8)
                c.locator.click(timeout=5000)
                clipped += 1
                progressed = True
                log(f"  clipped ({clipped}/{budget}) ${c.savings.value:.2f}: {c.label!r}")
                human_pause(args.min_delay, args.max_delay)
            except Exception as e:
                log(f"  skip {c.label!r}: {e}", debug=args.debug, is_debug_only=True)
                dismiss_modal(page, debug=args.debug)
            # limit safety net: a visible 'limit/maximum reached' message.
            try:
                warn = page.get_by_text(re.compile(r"(limit|maximum).{0,20}(reach|clip)",
                                                   re.I))
                if warn.count() and warn.first.is_visible():
                    log("Reached QFC's account clip limit; stopping.")
                    limit_hit = True
                    break
            except Exception:
                pass
        if not progressed:
            break
        human_pause(1.5, 2.5)

    log("\n" + "-" * 40)
    log(f"Done. Clipped {clipped} relevant coupon(s)"
        f"{' (account limit reached)' if limit_hit else ''}.")
    human_pause(5, 5)
    return 4 if limit_hit and clipped == 0 else 0


def main():
    ap = argparse.ArgumentParser(description="Auto-clip QFC digital coupons.")
    ap.add_argument("--debug", action="store_true", help="verbose output")
    ap.add_argument("--dry-run", action="store_true",
                    help="find clip buttons but do not click them")
    ap.add_argument("--max", type=int, default=0,
                    help="stop after clipping this many (0 = no limit)")
    ap.add_argument("--min-delay", type=float, default=1.2,
                    help="min seconds between clips (default 1.2)")
    ap.add_argument("--max-delay", type=float, default=3.0,
                    help="max seconds between clips (default 3.0)")
    ap.add_argument("--no-wait-login", action="store_true",
                    help="skip the 'press ENTER after login' prompt")
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
    if args.max:  # existing --max maps to max_clips
        overrides["max_clips"] = args.max
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
            print("\n" + "=" * 64)
            print("If you're not signed in: sign in and select your store now.")
            print("When the coupons are visible, come back here and press ENTER.")
            print("=" * 64)
            try:
                input()
            except EOFError:
                log("No interactive input; proceeding after a short wait.")
                human_pause(3, 4)

        # Close any modal that may be open before we start.
        dismiss_modal(page, debug=args.debug)

        # --- relevance mode: filter by department, rank by savings -----------
        if relevance_mode:
            matched, missing = select_departments(page, cfg.departments,
                                                  debug=args.debug)
            if missing:
                log(f"WARNING: these configured departments were not found: "
                    f"{missing}")
            if not matched:
                log("ERROR: none of the configured departments matched the "
                    "panel; aborting (set departments to valid names).")
                ctx.close()
                return 3
            log(f"Departments selected: {matched}")

            already = count_clipped_total(page, debug=args.debug)
            budget = max(0, cfg.max_clips - already)
            log(f"Budget: {budget} (cap {cfg.max_clips} - {already} already clipped)")
            if budget == 0:
                log("Already at the clip cap; nothing to do.")
                ctx.close()
                return 0

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
            if detect_logged_out(page):
                reason = "you appear to be SIGNED OUT of QFC"
            else:
                reason = "no coupons found (page may be blocked, empty, or changed)"
            log("\n" + "*" * 64)
            log(f"WARNING: {reason}.")
            log("Re-login needed: run this script interactively (WITHOUT "
                "--no-wait-login)")
            log(f"and sign in to refresh the saved session at {PROFILE_DIR}.")
            log("*" * 64)
            if args.no_wait_login:
                log("Scheduled run can't proceed; exiting with status 2.")
                ctx.close()
                return 2

        if relevance_mode:
            rc = _clip_relevant(page, cfg, budget, args)
            ctx.close()
            return rc

        clipped = 0
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
                try:
                    if not b.is_visible():
                        continue
                    b.scroll_into_view_if_needed(timeout=3000)
                    human_pause(0.3, 0.8)
                    if args.dry_run:
                        log(f"  [dry-run] would clip: {label!r}")
                    else:
                        b.click(timeout=5000)
                        clipped += 1
                        log(f"  clipped ({clipped}): {label!r}")
                        progressed = True
                        human_pause(args.min_delay, args.max_delay)
                except Exception as e:
                    log(f"  skip {label!r}: {e}", debug=args.debug, is_debug_only=True)
                    # A modal may have popped up and blocked the click; clear it.
                    dismiss_modal(page, debug=args.debug)

            if args.dry_run:
                break
            if args.max and clipped >= args.max:
                break
            if not progressed:
                break
            human_pause(1.5, 2.5)

        log("\n" + "-" * 40)
        if args.dry_run:
            log(f"Dry run complete. {len(candidates)} clippable coupon(s) detected.")
        else:
            log(f"Done. Clipped {clipped} coupon(s) across {rounds} pass(es).")
        log("Closing in 5 seconds...")
        human_pause(5, 5)
        ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
