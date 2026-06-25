# One-Click Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cloned-repo user run the clipper by double-clicking a launcher, with the clipper auto-detecting login instead of waiting for an ENTER keypress.

**Architecture:** Add a `wait_until_ready()` polling helper to `qfc_coupon_clipper.py` that reuses the existing `dismiss_modal` / `scan_coupon_buttons` / `detect_logged_out` helpers; it replaces the interactive `input()` gate as the default behavior (`--no-wait-login` still bypasses it). Add root-level `launch.sh` (shared logic, Linux entry) and `launch.command` (macOS Finder double-click) that bootstrap the venv on first run via `scripts/setup.sh`.

**Tech Stack:** Python 3.11+, Playwright (sync API), pytest, bash.

**Spec:** `docs/superpowers/specs/2026-06-24-one-click-launcher-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `qfc_coupon_clipper.py` (modify) | New `wait_until_ready()` helper; call-site swap from `input()` gate; docstring update |
| `tests/test_qfc_clipper.py` (modify) | Unit tests for `wait_until_ready()` (monkeypatched, no browser) |
| `launch.sh` (create, root) | Interactive launcher: first-run bootstrap → run clipper (visible) → pause |
| `launch.command` (create, root) | macOS double-click wrapper that execs `./launch.sh` |
| `README.md` (modify) | Quick-start (one-click) section; remove "press ENTER" from first-run |

Helpers reused (already defined, all before `main()`): `dismiss_modal(page, debug=False)`, `scan_coupon_buttons(page) -> (n_clip, n_clipped)`, `detect_logged_out(page) -> bool`. `time` is already imported at the top of `qfc_coupon_clipper.py`.

---

### Task 1: Add `wait_until_ready()` helper (TDD)

**Files:**
- Test: `tests/test_qfc_clipper.py` (append at end)
- Modify: `qfc_coupon_clipper.py` (insert after `scan_coupon_buttons`, i.e. after line 181, before `def count_clipped_total`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_qfc_clipper.py`:

```python

# --- wait_until_ready -------------------------------------------------------

def _patch_ready(monkeypatch, scan_results, logged_out=False):
    """Drive wait_until_ready with canned scan_coupon_buttons results.

    scan_results: list of (n_clip, n_clipped) tuples returned in order; the
    last entry repeats if the loop polls more times than provided.
    """
    state = {"i": 0}

    def fake_scan(page):
        i = min(state["i"], len(scan_results) - 1)
        state["i"] += 1
        return scan_results[i]

    monkeypatch.setattr(clipper, "scan_coupon_buttons", fake_scan)
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "detect_logged_out", lambda page: logged_out)
    monkeypatch.setattr(clipper.time, "sleep", lambda s: None)


def test_wait_until_ready_immediate(monkeypatch, capsys):
    _patch_ready(monkeypatch, [(3, 0)])
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is True
    assert "Sign in" not in capsys.readouterr().out


def test_wait_until_ready_after_login(monkeypatch, capsys):
    # First poll: nothing visible + logged out. Second poll: coupons appear.
    _patch_ready(monkeypatch, [(0, 0), (1, 0)], logged_out=True)
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is True
    assert capsys.readouterr().out.count("Sign in to QFC") == 1


def test_wait_until_ready_timeout(monkeypatch, capsys):
    _patch_ready(monkeypatch, [(0, 0)], logged_out=False)
    times = iter([0.0, 999.0, 999.0])
    monkeypatch.setattr(clipper.time, "monotonic", lambda: next(times))
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is False
    assert "Sign in" not in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_qfc_clipper.py -k wait_until_ready -v`
Expected: FAIL — `AttributeError: module 'qfc_coupon_clipper' has no attribute 'wait_until_ready'`

- [ ] **Step 3: Implement `wait_until_ready`**

Insert into `qfc_coupon_clipper.py` after `scan_coupon_buttons` (after line 181), before `def count_clipped_total`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_qfc_clipper.py -k wait_until_ready -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add qfc_coupon_clipper.py tests/test_qfc_clipper.py
git commit -m "Add wait_until_ready() poll helper with tests"
```

---

### Task 2: Wire `wait_until_ready` into the run flow (replace ENTER gate)

**Files:**
- Modify: `qfc_coupon_clipper.py:476-485` (the `input()` gate) and the module docstring (line 26)

- [ ] **Step 1: Replace the ENTER gate**

In `qfc_coupon_clipper.py`, replace these lines (476–485):

```python
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
```

with:

```python
        if not args.no_wait_login:
            if not wait_until_ready(page, debug=args.debug):
                print("\nCouldn't auto-detect your coupons. If you're signed in, press "
                      "ENTER to continue; otherwise sign in first, then ENTER "
                      "(Ctrl-C to quit).")
                try:
                    input()
                except EOFError:
                    log("No interactive input; proceeding.")
```

- [ ] **Step 2: Update the module docstring**

In `qfc_coupon_clipper.py`, replace line 26:

```
    3. Return to the terminal and press ENTER. The script takes over and clips.
```

with:

```
    3. The script detects when your coupons have loaded and starts on its own.
```

- [ ] **Step 3: Verify the module still imports and the CLI is intact**

Run: `.venv/bin/python qfc_coupon_clipper.py --help`
Expected: argparse help text prints (no import/NameError).

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (56 passed — the prior 53 plus the 3 new `wait_until_ready` tests).

- [ ] **Step 5: Commit**

```bash
git add qfc_coupon_clipper.py
git commit -m "Replace ENTER gate with auto-detect wait_until_ready"
```

---

### Task 3: Create the launcher scripts

**Files:**
- Create: `launch.sh` (root)
- Create: `launch.command` (root)

- [ ] **Step 1: Create `launch.sh`**

```bash
#!/usr/bin/env bash
# Interactive one-click launcher: bootstraps the venv on first run, then runs
# the clipper with visible output. For scheduled/unattended runs use
# scripts/run.sh --no-wait-login instead.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "First run — setting up (this can take a minute)…"
  ./scripts/setup.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python qfc_coupon_clipper.py "$@"

echo
read -r -p "Done — press ENTER to close. "
```

- [ ] **Step 2: Create `launch.command`**

```bash
#!/usr/bin/env bash
# macOS Finder double-click entry point. Runs the shared launcher from the
# repo root so a Terminal window opens with visible output.
cd "$(dirname "$0")" && exec ./launch.sh "$@"
```

- [ ] **Step 3: Make them executable**

Run: `chmod +x launch.sh launch.command`

- [ ] **Step 4: Syntax-check (and shellcheck if available)**

Run: `bash -n launch.sh && bash -n launch.command && echo OK`
Expected: `OK`

Run (optional): `command -v shellcheck >/dev/null && shellcheck launch.sh launch.command || echo "shellcheck not installed — skipped"`
Expected: no warnings, or the skip message.

- [ ] **Step 5: Confirm the executable bit is staged, then commit**

Run: `git add launch.sh launch.command && git ls-files -s launch.sh launch.command`
Expected: mode `100755` for both.

```bash
git commit -m "Add one-click launcher (launch.sh + launch.command)"
```

---

### Task 4: Update the README

**Files:**
- Modify: `README.md` — add Quick start section after the Setup code block; fix the first-run step 3.

- [ ] **Step 1: Add the Quick start section**

In `README.md`, immediately after the Setup code block's closing ```` ``` ```` (the line after `./scripts/setup.sh   # creates .venv, installs deps, pulls Playwright's Chromium`) and before `## First run`, insert:

```markdown

## Quick start (one-click)

Once you've cloned the repo (above), you don't need the manual steps below:

- **macOS:** double-click **`launch.command`** in Finder.
- **Linux:** run **`./launch.sh`**.

The first run installs everything automatically (it calls `scripts/setup.sh`), then
opens the browser. Sign in to QFC if you aren't already — clipping starts on its own
once your coupons load. (`launch.*` is the interactive path; `scripts/run.sh` is the
unattended/scheduled one.)
```

- [ ] **Step 2: Remove the "press ENTER" first-run step**

In `README.md`, replace:

```
3. Switch back to the terminal and press **ENTER**.
```

with:

```
3. It detects when you're signed in and your coupons have loaded, then starts
   automatically — no need to switch back and press ENTER.
```

- [ ] **Step 3: Sanity-check the rendered references**

Run: `grep -nE "launch\.(sh|command)|press \*\*ENTER\*\*" README.md`
Expected: the launcher is mentioned in the new section; the `press **ENTER**` line is gone.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document one-click launcher; drop manual ENTER step"
```

---

### Task 5: Final verification

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (56 passed).

- [ ] **Step 2: Launcher syntax**

Run: `bash -n launch.sh && bash -n scripts/run.sh && bash -n scripts/setup.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Manual smoke test (human, not automated)**

This needs a real browser + QFC session, so it is a manual check, not part of CI:
1. From the repo root, run `./launch.sh` (or double-click `launch.command` on macOS).
2. Confirm: the browser opens; if already signed in, clipping starts within a few seconds with **no ENTER**; if signed out, the sign-in prompt appears once and clipping starts automatically after you log in.
3. Confirm the Terminal window stays open at the end ("Done — press ENTER to close.").

- [ ] **Step 4: (If desired) finish the branch**

Use the finishing-a-development-branch skill to merge `one-click-launcher` into `main` (or open a PR).

---

## Notes

- `--no-wait-login` (used by `scripts/run.sh` for cron/launchd) is unchanged — it bypasses `wait_until_ready` entirely, and the existing logged-out handling at `qfc_coupon_clipper.py:523` still reports and exits for unattended runs.
- Timeout is a fixed 180s with 2.0s polling (per spec; configurable timeout is explicitly out of scope).
- Line numbers reference the repo at commit `2e58266`; re-confirm before editing.
