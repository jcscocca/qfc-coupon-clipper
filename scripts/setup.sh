#!/usr/bin/env bash
# Bootstrap a virtual environment for the QFC coupon clipper and install its
# dependencies (including Playwright's Chromium). Run once after cloning.
#
#   ./scripts/setup.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null 2>&1 || {
  echo "Python interpreter '$PY' not found. Install Python 3.11+ (or set PYTHON=...)." >&2
  exit 1
}

echo "Creating virtual environment in .venv ..."
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [[ -f requirements-dev.txt ]]; then
  echo "Installing dev deps from requirements-dev.txt ..."
  python -m pip install -r requirements-dev.txt
fi

# Playwright needs its browser binary.
echo "Installing Playwright Chromium ..."
python -m playwright install chromium

echo
echo "Done. Activate with:  source .venv/bin/activate"
echo "Then run:  python qfc_coupon_clipper.py"
