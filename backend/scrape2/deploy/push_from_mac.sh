#!/usr/bin/env bash
# Mac pe chalao — local poll karke JSON VPS pe bhejta hai (CF block bypass)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VPS="${VPS:-root@187.127.115.11}"
REMOTE="${REMOTE:-/var/www/diaapi/output}"

echo "==> Mac pe login + 1 poll cycle..."
.venv/bin/python api_login.py
POLL_ONCE=1 .venv/bin/python poll.py --once

echo "==> Upload output/ -> $VPS:$REMOTE"
scp -r output/*.json "$VPS:$REMOTE/"

echo "Done. Check: http://187.127.115.11:8080/"
