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
