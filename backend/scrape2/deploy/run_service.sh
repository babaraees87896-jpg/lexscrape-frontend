#!/usr/bin/env bash
# PM2 is script ko chalata hai — poll + HTTP server ek saath
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

export LOGIN_USER="${LOGIN_USER:-Demo9304}"
export LOGIN_PASS="${LOGIN_PASS:-Demo1234}"
export USE_FLARESOLVERR="${USE_FLARESOLVERR:-1}"
export FLARESOLVERR_URL="${FLARESOLVERR_URL:-http://127.0.0.1:8191/v1}"
PY="$ROOT/.venv/bin/python"

mkdir -p output logs

# CF bypass login
"$PY" api_login.py >> logs/login.log 2>&1 || true

# Poll background
"$PY" poll.py >> logs/poll.log 2>&1 &
POLL_PID=$!
"$PY" poll_sport.py >> logs/sport.log 2>&1 &
SPORT_PID=$!

cleanup() {
  kill "$POLL_PID" 2>/dev/null || true
  kill "$SPORT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# HTTP server foreground (PM2 is process ko monitor karega)
exec "$PY" serve.py --port "${PORT:-8080}" --directory output
