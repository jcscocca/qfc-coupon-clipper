# One-Click Launcher — Design

**Date:** 2026-06-24
**Status:** Approved in brainstorming; pending spec review

## Problem

Running the clipper today takes several manual terminal steps: install Python 3.11+,
clone, `./setup.sh`, `source .venv/bin/activate`, `python qfc_coupon_clipper.py`,
then watch for the coupons to appear and **press ENTER**. We want a one-click way
for people who have cloned the repo to run it without dealing with venvs, setup, or
remembering the ENTER step.

## Goal

A double-clickable launcher (macOS) / single executable (Linux) that, on first run,
bootstraps the environment, then runs the clipper. The clipper proceeds on its own
once the user is logged in — no ENTER step in the common case.

## Decisions (from brainstorming)

- **Audience:** people who have cloned the repo but shouldn't have to run `setup.sh`,
  activate a venv, or remember flags. A "smart launcher," not a packaged native app.
- **Platforms:** macOS + Linux (matches the repo's current stated support). No Windows.
- **Login flow:** auto-detect with fallback — poll until logged in and coupons are
  loaded, then proceed; show a sign-in prompt only when login is actually needed.
- **CLI wiring:** auto-detect becomes the **default** interactive behavior (one code
  path). `--no-wait-login` still bypasses it for cron/launchd.
- **Close prompt:** the launcher pauses with "press ENTER to close" at the end so the
  Terminal window doesn't vanish before the user reads the result.

## Components

### A. Auto-detect login — `qfc_coupon_clipper.py`

Replace the `input()` ENTER gate (current [lines 476–485]) with a new helper:

```
def wait_until_ready(page, *, timeout=180, poll=2.0, debug=False) -> bool:
    """Poll until the coupon grid is rendered (i.e. logged in), instead of
    blocking on ENTER. Shows a one-time sign-in prompt if we look logged out.
    Returns True if coupons appeared within `timeout`, else False."""
    # loop until a monotonic deadline:
    #   1. dismiss_modal(page)               # a modal must not suppress detection
    #   2. n_clip, n_clipped = scan_coupon_buttons(page)
    #      if n_clip + n_clipped > 0: return True     # positive signal wins
    #   3. if detect_logged_out(page) and not prompted:
    #          print one-time "sign in to QFC in the browser window" message
    #          prompted = True
    #   4. sleep(poll)
    # return False
```

Call site (replacing the gate, still **before** department selection at [line 491]):

```
if not args.no_wait_login:
    if not wait_until_ready(page, debug=args.debug):
        # fallback: let a human intervene
        print("Couldn't auto-detect coupons. Sign in, then press ENTER (Ctrl-C to quit).")
        try:
            input()
        except EOFError:
            pass
```

**Design notes:**
- **Positive signal wins.** Coupons-present short-circuits the loop regardless of any
  stray "Sign In" link, matching the existing comment at [line 517] that a stray Sign
  In control while coupons exist must not abort a good run. `detect_logged_out` is used
  only to decide whether to *show* the sign-in prompt, never to block a good session.
- **Modal dismissal inside the loop** so a promo / "Coupon Details" modal can't hide the
  coupon buttons and stall detection.
- **Ordering preserved.** `wait_until_ready` runs before `select_departments`
  ([line 298]), which requires the logged-in SPA to have rendered the Departments panel.
- **Clock:** use `time.monotonic()` for the deadline (not wall-clock).

**Behavior matrix:**

| Situation | Result |
|-----------|--------|
| Saved session valid | Coupons render in seconds → proceeds. No prompt, no ENTER. |
| Login needed | One-time sign-in prompt; proceeds automatically once logged in. |
| No login within 180s | Final ENTER prompt; control then falls through to the existing logged-out handling ([line 523]), which reports and exits. |
| `--no-wait-login` (cron) | Unchanged — skips all of this. |

### B. Launcher — `launch.sh` + `launch.command`

`launch.sh` (real logic; Linux entry point; `chmod +x`):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  echo "First run — setting up (this can take a minute)…"
  ./setup.sh
fi
source .venv/bin/activate
python qfc_coupon_clipper.py "$@"
echo
read -r -p "Done — press ENTER to close. "
```

`launch.command` (macOS Finder double-click; `chmod +x`):

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")" && exec ./launch.sh "$@"
```

**Relationship to `run.sh`:** intentionally separate.
- `run.sh` — unattended/cron/launchd; redirects output to `logs/qfc_clipper.log`;
  used with `--no-wait-login`.
- `launch.sh` / `launch.command` — interactive one-click; visible output; auto-setup;
  relies on the new auto-detect login.

### C. Docs — `README.md`

- Add a "Quick start (one-click)" section near the top: double-click `launch.command`
  (macOS) or run `./launch.sh` (Linux); first run sets everything up; sign in when the
  browser opens and it proceeds automatically.
- Update the existing "First run" steps (currently ~lines 28–40) to drop the "press
  ENTER" instruction in favor of "it proceeds automatically once you're signed in."
- Clarify `run.sh` is for scheduled/unattended use vs. `launch.*` for interactive use.

### D. Tests — `test_qfc_clipper.py`

Unit-test `wait_until_ready` by monkeypatching `scan_coupon_buttons`,
`detect_logged_out`, `dismiss_modal`, and the sleep/clock:
1. Coupons present on first poll → returns `True`, no prompt.
2. Logged out for a few polls then coupons appear → returns `True`, prompt shown once.
3. Never ready → returns `False` after the deadline (with a stubbed fast clock).

Launchers: `bash -n` syntax check + `shellcheck`. Full end-to-end (real browser + QFC
login) is a manual smoke test, not automated.

## Out of scope (YAGNI)

Windows launcher, Linux `.desktop` file, GUI/menu-bar app, configurable timeout. Deferred
unless requested.

## Files touched

| File | Type | Change |
|------|------|--------|
| `launch.sh` | new | Interactive launcher + first-run bootstrap |
| `launch.command` | new | macOS double-click wrapper |
| `qfc_coupon_clipper.py` | edit | Replace ENTER gate with `wait_until_ready()` |
| `README.md` | edit | Quick-start + first-run wording; run.sh vs launch.* |
| `test_qfc_clipper.py` | edit | Tests for `wait_until_ready()` |

Line references above are from the repo at design time (commit `955bfd6`) and should be
re-confirmed during implementation.
