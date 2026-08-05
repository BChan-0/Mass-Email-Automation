#!/usr/bin/env bash
# Start the draft builder UI on http://127.0.0.1:5000
# Creates a virtualenv and installs dependencies on first run.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-5000}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv"
  "$PYTHON" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi

echo "Open http://127.0.0.1:${PORT}"
exec .venv/bin/python -m app.web --port "$PORT" "$@"
