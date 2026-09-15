#!/usr/bin/env sh
# macOS / Linux launcher. No args = GUI; args go to the CLI (doctor / convert ...).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m pip install -q deezy 2>/dev/null || echo "deezy not installed (optional: DDP Atmos output needs it)"
if [ $# -eq 0 ]; then exec .venv/bin/python -m fpsdee gui; else exec .venv/bin/python -m fpsdee "$@"; fi
