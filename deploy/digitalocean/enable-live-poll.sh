#!/usr/bin/env bash
# Enable scrape2 live pollers on DigitalOcean (PM2 + optional FlareSolverr)
# Usage: cd /opt/lex && sudo bash deploy/digitalocean/enable-live-poll.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/lex}"
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: $APP_DIR/.env missing — copy deploy/digitalocean/env.example first"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

export APP_DIR="$APP_DIR"

# Default demo credentials — also fix empty LOGIN_PASS= in .env
if [[ -z "${LOGIN_PASS:-}" ]]; then
  export LOGIN_USER="${LOGIN_USER:-Demo9304}"
  export LOGIN_PASS="${LOGIN_PASS:-Demo1234}"
  if grep -q '^LOGIN_PASS=.' .env 2>/dev/null; then
    :
  elif grep -q '^LOGIN_PASS=' .env 2>/dev/null; then
    sed -i "s/^LOGIN_PASS=.*/LOGIN_PASS=${LOGIN_PASS}/" .env
    echo "  Updated empty LOGIN_PASS in .env"
  else
    echo "LOGIN_USER=${LOGIN_USER}" >> .env
    echo "LOGIN_PASS=${LOGIN_PASS}" >> .env
    echo "EX99_ENABLE_SPORT_POLL=1" >> .env
    echo "EX99_LIVE_MATCHES=1" >> .env
    echo "EX99_SCRAPE_LIVE_ONLY=1" >> .env
    echo "  Added default LOGIN_* to .env (Demo9304)"
  fi
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

mkdir -p logs backend/scrape2/output/sport

echo "=== pip: curl_cffi (sport poll) ==="
.venv/bin/pip install -q curl_cffi

echo "=== FlareSolverr (Cloudflare bypass) ==="
if command -v docker >/dev/null 2>&1; then
  if ! docker ps --format '{{.Names}}' | grep -qx flaresolverr; then
    docker rm -f flaresolverr 2>/dev/null || true
    docker run -d --restart unless-stopped \
      --name flaresolverr \
      -p 127.0.0.1:8191:8191 \
      -e LOG_LEVEL=info \
      ghcr.io/flaresolverr/flaresolverr:latest
    echo "  FlareSolverr started on 127.0.0.1:8191"
  else
    echo "  FlareSolverr already running"
  fi
else
  echo "  Docker not found — skip FlareSolverr (set USE_FLARESOLVERR=0 if login fails)"
fi

echo "=== Test poll (once) ==="
cd backend/scrape2
if ../../.venv/bin/python poll_sport.py --once; then
  echo "  Sport poll test OK"
else
  echo "  WARN: sport poll test failed — check LOGIN_USER/PASS and FlareSolverr"
fi
cd "$APP_DIR"

echo "=== PM2 live pollers ==="
pm2 delete lex-sport-poll lex-casino-poll 1ex-sport-poll 1ex-casino-poll 2>/dev/null || true
pm2 start deploy/digitalocean/ecosystem.live-poll.config.cjs
pm2 save

echo ""
echo "============================================"
echo "  LIVE POLL ENABLED"
echo "============================================"
echo "  pm2 logs lex-sport-poll"
echo "  tail -f logs/sport-poll.out.log"
echo "  Data: backend/scrape2/output/sport/"
echo "============================================"
