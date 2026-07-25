# qfc-coupon-clipper

Automatically clip your **QFC** digital coupons — every available one, or just the
ones worth clipping. It drives a real, visible Chromium browser (via Playwright)
using a persistent profile, so **you** log into your QFC account once and the script
never touches your password. Because it's a genuine browser, it gets past the bot
protection that blocks plain HTTP scrapers. Coupons you've already clipped are
detected and left untouched.

## Why a real browser

QFC (a Kroger banner) sits behind Akamai bot protection, and its coupon page is
rendered by JavaScript after login. A plain HTTP scraper gets blocked or sees an
empty page. Driving a genuine browser with Playwright sidesteps both problems, and
you sign in yourself so the script never handles your password. Your login is saved
to `~/.qfc_clipper_profile` (never committed) and reused on later runs.

## Setup

Requires Python 3.11+ (for the stdlib `tomllib`). Works on macOS and Linux.

```bash
git clone https://github.com/jcscocca/qfc-coupon-clipper.git
cd qfc-coupon-clipper
./scripts/setup.sh   # creates .venv, installs deps, pulls Playwright's Chromium
```

## Quick start (one-click)

Once you've cloned the repo (above), you don't need the manual steps below:

- **macOS:** double-click **`launch.command`** in Finder.
- **Linux:** run **`./launch.sh`**.

The first run installs everything automatically (it calls `scripts/setup.sh`), then
opens the browser. Sign in to QFC if you aren't already — clipping starts on its own
once your coupons load. After a successful clipping run, the launcher also imports
the latest **My Purchases** receipt into the verified-savings ledger. (`launch.*` is
the interactive path; `scripts/run.sh` is the unattended/scheduled coupon-only one.)

## First run

```bash
source .venv/bin/activate
python qfc_coupon_clipper.py
```

1. A browser window opens at the QFC coupons page.
2. Sign in and select your store if prompted. Your login is saved to
   `~/.qfc_clipper_profile` and reused next time.
3. It detects when you're signed in and your coupons have loaded, then starts
   automatically — no need to switch back and press ENTER.
4. It scrolls to load all coupons, then clips each one with short randomized pauses,
   printing progress. Any stray "Coupon Details" modal is auto-closed.

## Flags

| Flag | Effect |
|------|--------|
| `--debug` | Prints every button label it sees — use this to tune selectors. |
| `--dry-run` | Finds clip buttons and reports them, but clicks nothing. |
| `--max 25` | Stop after clipping 25 coupons. |
| `--min-delay` / `--max-delay` | Pause (seconds) between clips. Defaults 3.2–4.2. |
| `--no-wait-login` | Skip the "press ENTER" prompt — use this for scheduled runs. |
| `--config PATH` | Use a specific `config.toml` (default: `./config.toml`). |
| `--departments "Dairy,Produce"` | Override the configured departments. |
| `--min-savings 0.5` | Skip coupons below this (estimated) dollar value. |

## Clipping only relevant coupons (departments + savings)

QFC has been observed enforcing a maximum of **249 clipped coupons per account**.
To prioritize the most useful offers within that limit, you can restrict the first
pass to departments you shop and then fill the remaining slots in QFC's
relevance/popularity order.

```bash
cp config.example.toml config.toml      # then edit it
```

- `departments` — uncomment the aisles you shop (names must match QFC's left panel
  exactly). **Empty = clip everything (legacy behavior).**
- `max_clips` — account-wide ceiling (default `249`, matching QFC's observed
  maximum); the script subtracts already-clipped coupons. Set it to `0` only to
  rely on QFC's own limit response.
- `min_savings` — optional floor; skip coupons below this value.
- `include_nondollar` / `[estimates]` — BOGO and `% off` coupons get an assumed
  dollar value so they rank fairly (a BOGO defaults to $5, beating small coupons).
- `fill_to_limit` — when `true`, clip configured departments first, then clear the
  filters and clip the unfiltered list from the top down, preserving QFC's
  relevance/popularity order, until an optional `max_clips` ceiling, QFC's actual
  limit, or the available coupon list is reached.

A coupon is counted only after its button changes to QFC's clipped state. Repeated
unconfirmed clicks stop the run instead of inflating the success total. The default
pace stays below the site's observed burst threshold; lowering the delays can cause
QFC to silently reject otherwise valid clips.

Progress such as `30/205` means 30 confirmed clips from up to 205 remaining account
slots (249 minus 44 already clipped), capped by the coupons actually available. With
`max_clips = 0`, the run uses the fully loaded, unfiltered list as its working budget
and lets QFC enforce its actual account limit.

Preview before clipping — prints the ranked plan with `(est)` markers, clips nothing:

```bash
python qfc_coupon_clipper.py --debug --dry-run
```

## Verified savings from receipts

The receipt-savings command opens QFC **My Purchases**, selects the latest in-store
purchase, reads its online receipt, and records the verified receipt total locally:

```bash
python qfc_receipt_savings.py                 # latest purchase
python qfc_receipt_savings.py --date 2026-07-20
```

This runs automatically after every successful `launch.sh` / `launch.command` coupon
run. Calling `qfc_coupon_clipper.py` or the scheduled `scripts/run.sh` directly stays
coupon-only. The receipt import is idempotent, so repeated launcher runs do not
double-count the latest purchase.

It reports the amount paid, actual savings, savings percentage, and cumulative
savings across imported receipts. The ledger is stored at
`data/receipt_savings.json` and is gitignored because it contains personal purchase
history. Use `--output PATH` to store it elsewhere.

QFC's online receipt combines digital coupons, store promotions, BOGO offers, and
sale pricing under **Item Coupons/Sales**. The indicator therefore says **verified
receipt savings**; it does not claim that every dollar came from coupons clipped by
this tool. The importer also reconciles original item total minus savings plus fees
and tax against the paid total before recording a receipt.

## Scheduling

A job that drives a **visible browser** must run inside a logged-in desktop session.

### macOS — LaunchAgent (recommended)

A plain `cron` job on macOS runs in a background session that usually can't open a GUI
window, so a visible-browser job silently fails. Use a per-user LaunchAgent instead —
it runs inside your GUI session.

**Before installing, run the clipper interactively once** (`python qfc_coupon_clipper.py`) so your login is saved to `~/.qfc_clipper_profile` — otherwise the first scheduled run will find no session and exit with status `2`.

`scheduling/com.example.qfc-clipper.plist` runs the clipper every Wednesday at 8am.
Edit the `/ABSOLUTE/PATH/TO/...` paths (and the `Label` if you like), then:

```bash
cp scheduling/com.example.qfc-clipper.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.example.qfc-clipper 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.qfc-clipper.plist
launchctl list | grep qfc-clipper          # confirm it's registered
```

The Mac must be awake and you logged in for the browser to open; launchd runs a missed
job at the next opportunity after wake.

### Linux / headless — cron

```bash
crontab -e
# Clip QFC coupons every Wednesday at 8am:
0 8 * * 3 /ABSOLUTE/PATH/TO/qfc-coupon-clipper/scripts/run.sh --no-wait-login
```

`scripts/run.sh` activates the venv and appends output to `logs/qfc_clipper.log`.

If QFC logs your saved session out, a scheduled run exits with a clear "re-login
needed" message (status `2`) instead of silently clipping nothing — open the script
once and sign back in to refresh it. Running weekly usually keeps the session alive.

## Tests

The pure selector/parsing helpers have `pytest` coverage (no browser is launched):

```bash
source .venv/bin/activate
pytest
```

## If coupons aren't found / a modal won't close

Run `python qfc_coupon_clipper.py --debug --dry-run` and read the printed button
labels. Adjust `CLIP_TEXTS` / `CLIPPED_TEXTS` near the top of `qfc_coupon_clipper.py`
if the clip/clipped wording has changed.

## Disclaimer / Terms of Service

This is an independent, unofficial tool — not affiliated with or endorsed by QFC or
Kroger. Kroger's Terms of Service generally discourage automation. Use it only with
your **own personal account** and at a normal, human-like pace (the default delays do
this). Provided **as-is, without warranty**; you assume all responsibility for how you
use it. See [LICENSE](LICENSE).
