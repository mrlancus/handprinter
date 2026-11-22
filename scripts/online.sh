#!/usr/bin/env sh
# Handprinter ONLINE launcher (macOS/Linux): app + Cloudflare Tunnel behind your password. See docs/online.md.
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[heic,pdf]"
fi
command -v cloudflared >/dev/null 2>&1 || { echo "Install cloudflared first (macOS: brew install cloudflared). See docs/online.md"; exit 2; }
exec .venv/bin/python -m handprinter online
