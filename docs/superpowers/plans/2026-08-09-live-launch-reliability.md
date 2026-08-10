# Live-Launch Reliability Fix Plan

**Date:** 2026-08-09

## Goal

Make the one-click launcher recover from the QFC states observed during the
2026-08-09 live run, fill the configured coupon capacity reliably, rank current
non-dollar offers correctly, and report receipt reauthentication failures without
misstating them as an empty purchase history.

## Live findings

1. The coupons route initially rendered the signed-in account header while the
   store remained on `Loading` and the body said QFC could not find coupons. The
   clipper waited for three minutes and required manual intervention; one reload
   recovered the 301-coupon grid.
2. The preferred pass confirmed all 129 attempted clips, but the fill handoff
   immediately declared the remaining inventory exhausted. A subsequent dry run
   found 129 unclipped coupons and planned all 77 remaining account slots.
3. QFC's live non-dollar labels have expanded: a BOGO label included product text
   between `BUY ONE` and `GET 1 FREE`, percent labels said `Save 20% on ...`, and
   fixed-price offers such as `$2.99 QFC Butter` were indistinguishable from
   dollar-off savings. The old parser misranked all three forms.
4. `/mypurchases` required a fresh QFC sign-in even though the coupon page still
   had an authenticated account session. After the interactive window expired,
   the importer incorrectly summarized that state as “no purchases were found.”

## Planned fixes

### 1. Recover a transiently empty coupon page

- Detect QFC's explicit “not finding any coupons” state separately from a logged-
  out state.
- While waiting for the grid, perform a small, bounded number of automatic page
  reloads when that transient state persists.
- Never reload over an active sign-in flow.
- Keep the existing final interactive fallback when neither coupons nor a
  recognized transient state can be resolved.

**Acceptance:** the observed empty/store-loading page recovers without requiring a
terminal ENTER after QFC becomes healthy; reload attempts are bounded and tested.

### 2. Make filter clearing and the fill transition observable

- Recognize both legacy `Clear All` controls and QFC's current accessible labels,
  such as `Clear all selected Departments filters`.
- Re-resolve controls after every click because each filter update can replace the
  DOM nodes.
- Verify that checked filter controls are actually cleared instead of treating a
  no-op click attempt as success.
- Preserve the initial unfiltered inventory total. Before the fill phase, fully
  load and rescan the unfiltered page; if its inventory is smaller than the known
  baseline, reload and retry a bounded number of times.
- If the baseline still cannot be verified, keep already-confirmed preferred clips
  but report the fill as skipped/incomplete. Do not claim all coupons were
  exhausted.

**Acceptance:** a stale preferred-only grid cannot produce a successful “all
available coupons exhausted” summary; the fill phase either sees the verified
unfiltered inventory or reports an explicit incomplete refresh.

### 3. Parse current non-dollar and fixed-price wording

- Match numeric and word quantities with punctuation and bounded intervening
  product text between `BUY ...` and `GET ... FREE`.
- Recognize both `20% off` and QFC's current `Save 20% on ...` form.
- Treat a bare dollar amount as a sale price unless it is explicitly introduced by
  `Save` or followed by `off`/`coupon`; use the neutral fallback estimate because
  the original price is unavailable.
- Preserve the existing precedence: an explicit dollar value still wins over an
  estimate.

**Acceptance:** the live Gatorade label ranks as a BOGO at the configured estimate,
current percent labels use the assumed-item-price estimate, and fixed shelf prices
are not mistaken for savings amounts.

### 4. Report receipt authentication accurately

- Centralize detection of QFC/Kroger login URLs.
- Keep the browser open for interactive sign-in when allowed, with wording that
  explains My Purchases may require fresh authentication even when coupons work.
- In non-interactive mode, return promptly when the login page is detected.
- If the wait expires on the login page, report incomplete reauthentication and
  explicitly state that the ledger was not updated; reserve “no purchases” for an
  authenticated purchases page that is genuinely empty.
- Make the launcher distinguish successful coupon clipping from a receipt-import
  warning while retaining a nonzero overall status for an incomplete one-click
  run.

**Acceptance:** no credential is entered or stored by the scripts, and an expired
receipt login produces actionable, truthful output.

### 5. Verification and documentation

- Add unit regressions for transient-page reloads, current clear-filter labels,
  verified fill inventory, BOGO product-text labels, and receipt login outcomes.
- Run the full pytest suite and shell syntax checks.
- Run a live `--dry-run` against the saved profile. It must make no coupon or
  ledger changes and must expose a complete plan for the remaining capacity.
- Update the README with automatic recovery and the possible My Purchases
  reauthentication step.

## Non-goals

- Automating, retrieving, or storing QFC credentials.
- Bypassing a QFC reauthentication requirement.
- Changing the configured departments, account cap, clip pacing, or receipt
  accounting rules.
- Performing additional live coupon clicks as part of verification.
