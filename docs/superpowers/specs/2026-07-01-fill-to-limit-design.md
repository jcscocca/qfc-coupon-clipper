# Fill-to-Limit Design

**Date:** 2026-07-01

## Problem

In relevance mode, the clipper currently treats configured departments as a hard
filter. It prints progress such as `68/206`, but `206` is the estimated remaining
account capacity (`max_clips - already_clipped`), not the number of eligible coupons
found. When the filtered candidate pool is exhausted, `_clip_relevant` stops on its
no-progress guard even if unused account capacity remains.

The capacity estimate is also inaccurate because `count_clipped_total` runs after
department filters are applied and before the full list is loaded. It therefore
counts only already-clipped coupons in the filtered, currently rendered view.

## Desired Behavior

Add an opt-in `fill_to_limit` setting. When enabled, configured departments are
preferences rather than a hard boundary:

1. Start from an unfiltered, fully loaded coupon list and count already-clipped
   coupons account-wide.
2. Compute the remaining capacity from that count.
3. Select and rank coupons from configured departments, then clip those first.
4. If capacity remains after preferred coupons are exhausted, clear the department
   filters, reload the full list, rank all remaining coupons, and continue clipping.
5. Stop when the configured cap is reached, QFC reports its account limit, or a fresh
   unfiltered scan finds no remaining clippable coupons.

The repository's gitignored local `config.toml` will enable `fill_to_limit = true` so
the one-click launcher gets the requested behavior. The checked-in example and
default remain `false` to avoid silently changing the meaning of existing users'
department allowlists.

## Configuration

Add `fill_to_limit: bool = False` to `relevance.Config` and load it from TOML.
Document it in `config.example.toml` and the README:

```toml
# Clip preferred departments first, then use any remaining coupons to fill capacity.
fill_to_limit = false
```

No new command-line flag is needed. The existing `--max` override continues to set
the cap, and `fill_to_limit` controls only whether the fallback phase runs.

## Runtime Flow

### 1. Establish an account-wide budget

Extract the existing "Clear All" behavior into a reusable `clear_filters(page)`
helper. Before selecting departments:

- Clear persisted filters.
- Scroll until the unfiltered list is fully loaded.
- Count visible already-clipped coupon controls.
- Set `budget = max(0, max_clips - already_clipped)`.

The log will call this value `remaining capacity`, not a coupon total. This is still
a best-effort browser-DOM count; QFC's own visible limit warning remains the final
safety net.

### 2. Preferred phase

Apply configured department filters, fully load the filtered result, and run the
existing savings ranking. The clipping loop will return a structured result
containing:

- the number of clicks made,
- whether QFC's limit warning appeared, and
- whether the available candidate pool was exhausted.

The loop will accept a shared set of labels already attempted during this run so the
fallback phase cannot immediately re-click a coupon whose button has not yet changed
to "Unclip."

### 3. Fill phase

If `fill_to_limit` is enabled, no limit warning appeared, and preferred clips used
less than the remaining capacity:

- Clear filters.
- Wait for the coupon grid to refresh.
- Fully load the unfiltered list again.
- Clip remaining coupons in descending estimated savings order.

The fill phase deliberately disables `min_savings` and `include_nondollar`
exclusions. Otherwise those filters could make "fill to limit" stop below the limit.
Savings parsing is still used to choose the best remaining coupons first.

If the refreshed unfiltered list has fewer coupons than the available capacity, the
run ends normally and explicitly reports that all available coupons were exhausted
below the cap.

### 4. Dry runs

Dry-run mode performs the same unfiltered counting and filter transitions, but never
clicks coupons. It reports the preferred plan and, when enabled, an unfiltered fill
plan without counting preferred labels twice.

## Error and Stop Handling

- A QFC limit/maximum warning stops both phases immediately.
- Failure to clear filters aborts the fill phase with a clear warning rather than
  claiming that the cap was reached.
- A pass with no newly eligible labels triggers one fresh scroll/rescan before the
  candidate pool is declared exhausted.
- The final summary distinguishes:
  - configured capacity reached,
  - QFC account limit reached, and
  - all available coupons exhausted below the cap.
- Existing sign-in, modal dismissal, and department-not-found behavior is retained.

## Code Shape

- `relevance.py`
  - Add and load `Config.fill_to_limit`.
- `qfc_coupon_clipper.py`
  - Add reusable filter-clearing logic.
  - Replace the integer-only clipping return value with a small result object.
  - Share attempted-label state between preferred and fill phases.
  - Move account-wide counting ahead of department selection.
  - Orchestrate the optional fill phase and improve progress/summary wording.
- `tests/test_relevance.py`
  - Cover the new default and TOML setting.
- `tests/test_qfc_clipper.py`
  - Cover unfiltered counting before department selection through extracted helpers.
  - Cover preferred-first then fill behavior.
  - Cover no duplicate attempts across phases.
  - Cover exhaustion below the cap and QFC-limit stop behavior.
- `README.md`, `config.example.toml`, and local `config.toml`
  - Document and enable the requested mode.

## Non-Goals

- Discovering a private QFC API for an authoritative account-wide count.
- Changing the configured 250-coupon cap.
- Retrying rejected clicks or proving server-side acceptance beyond the existing DOM
  state and visible QFC warning.
- Changing which departments are configured as preferred.
