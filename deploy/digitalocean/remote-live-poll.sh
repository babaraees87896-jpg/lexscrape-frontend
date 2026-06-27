#!/usr/bin/env bash
# One-liner remote install (run ON the droplet as root):
#   curl -fsSL "https://raw.githubusercontent.com/babaraees87896-jpg/lexscrape-frontend/main/deploy/digitalocean/remote-live-poll.sh" | bash
set -euo pipefail

RAW="${RAW_BASE:-https://raw.githubusercontent.com/babaraees87896-jpg/lexscrape-frontend/main}"
REPO="${GIT_REPO:-https://github.com/babaraees87896-jpg/lexscrape-frontend.git}"
BRANCH="${GIT_BRANCH:-main}"

find_app_dir() {
  for d in "${APP_DIR:-}" /opt/lex /opt/1ex; do
    [[ -n "$d" && -f "$d/backend/serve_local.py" ]] || continue
    echo "$d"
    return 0
  done
  return 1
}

sync_deploy_scripts() {
  local dir="$1"
  mkdir -p "$dir/deploy/digitalocean"
  for f in enable-live-poll.sh ecosystem.live-poll.config.cjs; do
    curl -fsSL "$RAW/deploy/digitalocean/$f" -o "$dir/deploy/digitalocean/$f"
    chmod +x "$dir/deploy/digitalocean/enable-live-poll.sh" 2>/dev/null || true
  done
}

echo "=== 1ex live poll remote setup ==="

APP_DIR="$(find_app_dir || true)"

if [[ -z "${APP_DIR:-}" ]]; then
  for d in /opt/lex /opt/1ex; do
    if [[ -d "$d" ]]; then
      APP_DIR="$d"
      echo "Using existing directory $APP_DIR (no serve_local.py check)"
      sync_deploy_scripts "$APP_DIR"
      break
    fi
  done
fi

if [[ -z "${APP_DIR:-}" ]]; then
  APP_DIR="/opt/lex"
  echo "No existing app — cloning into $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --branch "$BRANCH" --depth 1 "$REPO" "$APP_DIR"
elif [[ ! -d "$APP_DIR/.git" ]]; then
  echo "App found at $APP_DIR (not a git repo) — syncing deploy scripts only"
  sync_deploy_scripts "$APP_DIR"
else
  echo "App found at $APP_DIR — git pull"
  cd "$APP_DIR"
  git fetch origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

export APP_DIR
cd "$APP_DIR"
sync_deploy_scripts "$APP_DIR"
bash deploy/digitalocean/enable-live-poll.sh
pm2 restart 1ex-main 2>/dev/null || pm2 restart lex-main 2>/dev/null || true
pm2 status
