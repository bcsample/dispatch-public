#!/usr/bin/env bash
# Open Window (v1) launcher — the always-on, LLM-free live dashboard.
# Usage: ./run_window.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "venv missing — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
exec "$PY" -m brief.window
