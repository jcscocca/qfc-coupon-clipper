#!/usr/bin/env bash
# Interactive one-click launcher: bootstraps the venv on first run, runs the
# clipper, then imports the latest My Purchases receipt into the savings ledger.
# For scheduled/unattended coupon runs use scripts/run.sh --no-wait-login instead.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "First run — setting up (this can take a minute)…"
  ./scripts/setup.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

set +e
python qfc_coupon_clipper.py "$@"
clipper_rc=$?
set -e

receipt_rc=0
if [[ $clipper_rc -eq 0 ]]; then
  echo
  echo "Checking the latest QFC receipt savings…"
  set +e
  python qfc_receipt_savings.py
  receipt_rc=$?
  set -e
  if [[ $receipt_rc -ne 0 ]]; then
    echo "Receipt savings check failed (exit $receipt_rc)."
  fi
else
  echo
  echo "Coupon clipper failed (exit $clipper_rc); skipping the receipt check."
fi

echo
read -r -p "Done — press ENTER to close. " || true

if [[ $clipper_rc -ne 0 ]]; then
  exit "$clipper_rc"
fi
exit "$receipt_rc"
