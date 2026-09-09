#!/usr/bin/env bash
# One-liner dev runner. Set HUB_TOKEN first (see README).
set -euo pipefail
cd "$(dirname "$0")"
: "${HUB_DB:=hub.db}"
if [ ! -f .venv/bin/activate ]; then
  echo "→ creating venv"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python bot/main.py --db "$HUB_DB" --check
if [ -z "${HUB_TOKEN:-}" ]; then
  echo
  echo "✗ HUB_TOKEN not set. This is expected in check mode."
  echo "  export HUB_TOKEN=your-token-here && ./run.sh"
  exit 0
fi
exec python bot/main.py --db "$HUB_DB"
