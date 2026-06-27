#!/usr/bin/env bash
# Ek command: login + live JSON server
# Dashboard: http://localhost:8080/

set -e
cd "$(dirname "$0")"
PY=".venv/bin/python"
PORT="${PORT:-8080}"
export LOGIN_USER="${LOGIN_USER:-Demo9304}"
export LOGIN_PASS="${LOGIN_PASS:-Demo1234}"
export USE_FLARESOLVERR="${USE_FLARESOLVERR:-0}"
export POLL_INTERVAL="${POLL_INTERVAL:-5}"
export GAME_DELAY="${GAME_DELAY:-0.12}"

mkdir -p output

if [[ ! -x "$PY" ]]; then
  echo "venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Optional: DrissionPage browser cookies (Cloudflare)
if [[ "${1:-}" == "browser" ]]; then
  "$PY" drission_login.py --username "$LOGIN_USER" --password "$LOGIN_PASS"
fi

# API login + cookies save
echo "API login ($LOGIN_USER)..."
"$PY" api_login.py --username "$LOGIN_USER" --password "$LOGIN_PASS" || echo "API login skip — poll khud login karega"

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 0.5
fi

pkill -f "$(pwd)/poll.py" 2>/dev/null || true
pkill -f "$(pwd)/poll_sport.py" 2>/dev/null || true

cleanup() {
  [[ -n "${POLL_PID:-}" ]] && kill "$POLL_PID" 2>/dev/null || true
  [[ -n "${SPORT_PID:-}" ]] && kill "$SPORT_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Live poll start (casino + sports)..."
"$PY" poll.py &
POLL_PID=$!
"$PY" poll_sport.py &
SPORT_PID=$!
sleep 2

echo ""
echo "Casino:  http://localhost:${PORT}/"
echo "Sports:  http://localhost:${PORT}/sport/"
echo "Casino JSON: http://localhost:${PORT}/all_games.json"
echo "Sport JSON:  http://localhost:${PORT}/sport/all_sports.json"
echo "Stop: Ctrl+C"
exec "$PY" serve.py --port "$PORT" --directory output
