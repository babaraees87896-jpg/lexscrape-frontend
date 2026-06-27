#!/usr/bin/env bash
# One-liner remote install (run ON the droplet as root):
#   curl -fsSL "https://raw.githubusercontent.com/babaraees87896-jpg/lexscrape-frontend/main/deploy/digitalocean/remote-live-poll.sh" | APP_DIR=/opt/lex bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/lex}"
REPO="${GIT_REPO:-https://github.com/babaraees87896-jpg/lexscrape-frontend.git}"
BRANCH="${GIT_BRANCH:-main}"

echo "=== 1ex live poll remote setup ==="
echo "APP_DIR=$APP_DIR"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "ERROR: $APP_DIR not a git repo"
  exit 1
fi

cd "$APP_DIR"
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

bash deploy/digitalocean/enable-live-poll.sh
pm2 restart 1ex-main 2>/dev/null || true
pm2 status
