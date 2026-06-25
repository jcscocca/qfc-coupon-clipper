#!/usr/bin/env bash
# Run the QFC clipper using the local venv. Designed for cron/launchd: activates
# the environment, runs the clipper, and appends output to logs/qfc_clipper.log.
#
#   ./scripts/run.sh [args passed to qfc_coupon_clipper.py...]
#   ./scripts/run.sh --no-wait-login        # for scheduled runs
#
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs
LOG="logs/qfc_clipper.log"

if [[ ! -d .venv ]]; then
  echo "No .venv found — run ./scripts/setup.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "===== $(date '+%Y-%m-%d %H:%M:%S') running qfc_clipper =====" >> "$LOG"
# Don't let `set -e` abort before we record the exit code: capture it, log it,
# then propagate it so cron (and `$?`) see real failures.
set +e
python qfc_coupon_clipper.py "$@" >> "$LOG" 2>&1
rc=$?
set -e
echo "===== $(date '+%Y-%m-%d %H:%M:%S') finished qfc_clipper (exit $rc) =====" >> "$LOG"
exit "$rc"
