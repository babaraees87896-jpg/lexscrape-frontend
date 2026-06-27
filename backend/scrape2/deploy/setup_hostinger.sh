#!/usr/bin/env bash
# Hostinger VPS — FlareSolverr (Cloudflare bypass) + PM2
# Run: sudo bash deploy/setup_hostinger.sh

set -e
APP="/var/www/diaapi"
cd "$APP"

echo "=== [1/6] Docker check ==="
if ! command -v docker >/dev/null 2>&1; then
  apt update
  apt install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker
  systemctl start docker
fi
echo "Docker OK"

echo "=== [2/6] FlareSolverr start ==="
docker rm -f flaresolverr 2>/dev/null || true
docker run -d \
  --name flaresolverr \
  --restart unless-stopped \
  -p 127.0.0.1:8191:8191 \
  -e LOG_LEVEL=info \
  ghcr.io/flaresolverr/flaresolverr:latest

sleep 8
curl -s -X POST http://127.0.0.1:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd":"sessions.create"}' | head -c 100
echo ""
echo "FlareSolverr OK (port 8191)"

echo "=== [3/6] Playwright browser (fallback) ==="
if [[ -x .venv/bin/python ]]; then
  .venv/bin/pip install -q playwright 2>/dev/null || true
  .venv/bin/playwright install chromium 2>/dev/null || true
  .venv/bin/playwright install-deps chromium 2>/dev/null || apt install -y libnss3 libatk1.0-0 libgbm1 2>/dev/null || true
fi

echo "=== [4/6] Diagnose ==="
export USE_FLARESOLVERR=1
export FLARESOLVERR_URL=http://127.0.0.1:8191/v1
.venv/bin/python deploy/diagnose.py || true

echo "=== [5/6] PM2 restart ==="
pm2 delete diaapi 2>/dev/null || true
export USE_FLARESOLVERR=1
export FLARESOLVERR_URL=http://127.0.0.1:8191/v1
pm2 start deploy/ecosystem.config.cjs
pm2 save

echo "=== [6/6] Test JSON ==="
sleep 12
curl -s http://127.0.0.1:8080/all_games.json | head -c 400
echo ""
echo ""
echo "DONE — http://187.127.115.11:8080/all_games.json"
