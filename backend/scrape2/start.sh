#!/usr/bin/env bash
# =============================================================================
#  AllPanelExch Casino Live Scraper — ONE COMMAND SETUP + RUN
#
#  Naye system pe:
#    1. Poora scrape folder copy karo
#    2. Terminal: chmod +x start.sh && ./start.sh
#
#  Optional:
#    LOGIN_USER=MyId LOGIN_PASS=MyPass ./start.sh
#    ./start.sh browser          # DrissionPage se browser login (Cloudflare)
#    PORT=9000 ./start.sh        # alag port
#
#  URLs (default):
#    Dashboard : http://localhost:8080/
#    All games : http://localhost:8080/all_games.json
#    Single    : http://localhost:8080/vtrio_data.json
#
#  Games: 103 total (search in dashboard)
#  Stop : Ctrl+C
# =============================================================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export LOGIN_USER="${LOGIN_USER:-Demo9304}"
export LOGIN_PASS="${LOGIN_PASS:-Demo1234}"
export USE_FLARESOLVERR="${USE_FLARESOLVERR:-0}"
export POLL_INTERVAL="${POLL_INTERVAL:-5}"
export GAME_DELAY="${GAME_DELAY:-0.12}"
PORT="${PORT:-8080}"
PY="$ROOT/.venv/bin/python"
PIP="$ROOT/.venv/bin/pip"

echo ""
echo "=============================================="
echo "  Casino Live Scraper — Setup + Run"
echo "=============================================="
echo ""

# --- Step 1: Python ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 nahi mila."
  echo "  Mac:   brew install python3"
  echo "  Linux: sudo apt install python3 python3-venv python3-pip"
  exit 1
fi
echo "[1/6] Python: $(python3 --version)"

# --- Step 2: Virtual env ---
if [[ ! -x "$PY" ]]; then
  echo "[2/6] Virtual env bana rahe hain (.venv)..."
  python3 -m venv .venv
else
  echo "[2/6] Virtual env OK (.venv)"
fi

# --- Step 3: Dependencies ---
echo "[3/6] Dependencies install (pehli baar 1-2 min lag sakta hai)..."
"$PIP" install -q --upgrade pip
"$PIP" install -q -r requirements.txt
echo "      packages OK"

# --- Step 4: Output folder ---
mkdir -p output
echo "[4/6] output/ folder ready"

# --- Step 5: Login ---
echo "[5/6] Login ($LOGIN_USER)..."

if [[ "${1:-}" == "browser" ]]; then
  echo "      DrissionPage browser login..."
  "$PY" drission_login.py --username "$LOGIN_USER" --password "$LOGIN_PASS" || true
else
  "$PY" api_login.py --username "$LOGIN_USER" --password "$LOGIN_PASS" 2>/dev/null || {
    echo "      API login skip — poll apne cookies se try karega"
  }
fi

if [[ ! -f cookies.txt ]]; then
  echo "      WARNING: cookies.txt nahi bani — poll phir bhi login try karega"
fi

# --- Step 6: Purana server band + naya start ---
if command -v lsof >/dev/null 2>&1 && lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "      Port $PORT pe purana process band kar rahe hain..."
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 0.5
fi

pkill -f "$ROOT/poll.py" 2>/dev/null || true
pkill -f "$ROOT/poll_sport.py" 2>/dev/null || true

cleanup() {
  [[ -n "${POLL_PID:-}" ]] && kill "$POLL_PID" 2>/dev/null || true
  [[ -n "${SPORT_PID:-}" ]] && kill "$SPORT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[6/6] Live poll start (103 casino + sports)..."
"$PY" poll.py &
POLL_PID=$!
"$PY" poll_sport.py &
SPORT_PID=$!
sleep 3

GAME_COUNT=$("$PY" -c "from games import GAME_TYPES; print(len(GAME_TYPES))" 2>/dev/null || echo 103)

echo ""
echo "=============================================="
echo "  LIVE SERVER CHAL RAHA HAI"
echo "=============================================="
echo ""
echo "  Docs      : http://localhost:${PORT}/docs/"
echo "  Login     : http://localhost:${PORT}/login/"
echo "  Hub       : http://localhost:${PORT}/"
echo "  Admin     : http://localhost:${PORT}/admin/"
echo "  Casino    : http://localhost:${PORT}/casino/"
echo "  Sports    : http://localhost:${PORT}/sport/"
echo "  Matches   : http://localhost:${PORT}/sport/matches/"
echo "  Scorecard : http://localhost:${PORT}/sport/scorecard/"
echo "  All JSON  : http://localhost:${PORT}/all_games.json"
echo "  Sport JSON: http://localhost:${PORT}/sport/all_sports.json"
echo ""
echo "  Games: ${GAME_COUNT} total (dashboard mein search karo)"
echo "  Pehla full cycle ~15-20 sec lagta hai"
echo ""
echo "  JSON fields (har game):"
echo "    mid=Round ID | lt=Timer | gstatus=OPEN/SUSPENDED"
echo ""
echo "  Login change:"
echo "    DG_ADMIN_USER=admin DG_ADMIN_PASS=yourpass ./start.sh"
echo ""
echo "  Scraper login change:"
echo "    LOGIN_USER=ID LOGIN_PASS=PASS ./start.sh"
echo ""
echo "  Band karne ke liye: Ctrl+C"
echo "=============================================="
echo ""

exec "$PY" serve.py --port "$PORT" --directory output
