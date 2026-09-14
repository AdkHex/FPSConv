#!/usr/bin/env sh
# Run from source (macOS / Linux / Windows-with-bash). No args = GUI; args go to the CLI.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
if [ $# -eq 0 ]; then exec .venv/bin/python -m fpsconv gui; else exec .venv/bin/python -m fpsconv "$@"; fi
