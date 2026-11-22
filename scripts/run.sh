#!/usr/bin/env sh
# Handprinter launcher for Linux/macOS/WSL. First run creates .venv and installs the app.
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[heic,pdf]"
fi
exec .venv/bin/python -m handprinter run
