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
./setup.sh        # creates .venv, installs deps, pulls Playwright's Chromium
```

## First run

```bash
source .venv/bin/activate
python qfc_coupon_clipper.py
```

1. A browser window opens at the QFC coupons page.
2. Sign in and select your store if prompted. Your login is saved to
   `~/.qfc_clipper_profile` and reused next time.
3. Switch back to the terminal and press **ENTER**.
4. It scrolls to load all coupons, then clips each one with short randomized pauses,
   printing progress. Any stray "Coupon Details" modal is auto-closed.

## Flags

| Flag | Effect |
|------|--------|
| `--debug` | Prints every button label it sees — use this to tune selectors. |
| `--dry-run` | Finds clip buttons and reports them, but clicks nothing. |
| `--max 25` | Stop after clipping 25 coupons. |
| `--min-delay` / `--max-delay` | Pause (seconds) between clips. Defaults 1.2–3.0. |
| `--no-wait-login` | Skip the "press ENTER" prompt — use this for scheduled runs. |
| `--config PATH` | Use a specific `config.toml` (default: `./config.toml`). |
| `--departments "Dairy,Produce"` | Override the configured departments. |
| `--min-savings 0.5` | Skip coupons below this (estimated) dollar value. |

## Clipping only relevant coupons (departments + savings)

By default the clipper clips every coupon. QFC caps an account at ~150 clipped
coupons, so to spend that budget well you can restrict it to the departments you shop
and let it prioritize the highest-savings coupons.

```bash
cp config.example.toml config.toml      # then edit it
```

- `departments` — uncomment the aisles you shop (names must match QFC's left panel
  exactly). **Empty = clip everything (legacy behavior).**
- `max_clips` — cap (default 150); the script subtracts already-clipped coupons.
- `min_savings` — optional floor; skip coupons below this value.
- `include_nondollar` / `[estimates]` — BOGO and `% off` coupons get an assumed
  dollar value so they rank fairly (a BOGO defaults to $5, beating small coupons).

Preview before clipping — prints the ranked plan with `(est)` markers, clips nothing:

```bash
python qfc_coupon_clipper.py --debug --dry-run
```

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
0 8 * * 3 /ABSOLUTE/PATH/TO/qfc-coupon-clipper/run.sh --no-wait-login
```

`run.sh` activates the venv and appends output to `logs/qfc_clipper.log`.

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
