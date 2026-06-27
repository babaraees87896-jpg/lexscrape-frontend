#!/usr/bin/env bash
# DigitalOcean Droplet — 1ex.in backend (MongoDB + 4 Python APIs + Nginx)
#
# Usage (as root on fresh Ubuntu 22.04/24.04):
#   export GIT_REPO=https://github.com/babaraees87896-jpg/lexscrape-frontend.git
#   curl -fsSL "https://raw.githubusercontent.com/babaraees87896-jpg/lexscrape-frontend/main/deploy/digitalocean/setup.sh" | bash
# Or after git clone:
#   cd /opt/1ex && sudo bash deploy/digitalocean/setup.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/1ex}"
GIT_REPO="${GIT_REPO:-https://github.com/babaraees87896-jpg/lexscrape-frontend.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"

echo "============================================"
echo "  1ex.in — DigitalOcean backend setup"
echo "  APP_DIR=$APP_DIR"
echo "============================================"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl nginx python3 python3-venv python3-pip gnupg ca-certificates

# Node.js + PM2
if ! command -v pm2 >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
  npm install -g pm2
fi

# MongoDB 8.x (official repo)
if ! command -v mongod >/dev/null 2>&1; then
  curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
  CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-jammy}")"
  echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu ${CODENAME}/mongodb-org/8.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-8.0.list
  apt-get update -qq
  apt-get install -y -qq mongodb-org
  systemctl enable mongod
  systemctl start mongod
  sleep 3
fi

# App code
mkdir -p "$(dirname "$APP_DIR")"
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$GIT_BRANCH" --depth 1 "$GIT_REPO" "$APP_DIR"
else
  cd "$APP_DIR"
  git fetch origin "$GIT_BRANCH"
  git reset --hard "origin/$GIT_BRANCH"
fi

cd "$APP_DIR"

# Python venv
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r backend/requirements.txt

mkdir -p logs saved_state secrets

# Environment file
if [[ ! -f .env ]]; then
  cp deploy/digitalocean/env.example .env
  echo ""
  echo "  Created $APP_DIR/.env — edit WNP9_PASSWORD before going live."
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

# MongoDB restore (if backup present and DB empty)
USER_COUNT="$(.venv/bin/python -c "
from pymongo import MongoClient
c = MongoClient('${EX99_MONGO_URI:-mongodb://127.0.0.1:27017}', serverSelectionTimeoutMS=5000)
print(c['${EX99_MONGO_DB:-ex99_local}']['users'].count_documents({}))
" 2>/dev/null || echo 0)"

if [[ -f saved_state/mongo_backup.json ]] && [[ "${USER_COUNT}" == "0" ]]; then
  echo "  Restoring MongoDB from saved_state/mongo_backup.json ..."
  .venv/bin/python restore_mongodb_backup.py
elif [[ "${USER_COUNT}" == "0" ]]; then
  echo "  WARN: No mongo_backup.json — upload saved_state/mongo_backup.json then run:"
  echo "    cd $APP_DIR && .venv/bin/python restore_mongodb_backup.py"
fi

# Nginx
NGINX_CONF="/etc/nginx/sites-available/1ex-api"
cp deploy/digitalocean/nginx-1ex.conf "$NGINX_CONF"
sed -i "s/__API_MAIN__/${API_MAIN_DOMAIN:-api.1ex.in}/g" "$NGINX_CONF"
sed -i "s/__API_ADMIN__/${API_ADMIN_DOMAIN:-api-admin.1ex.in}/g" "$NGINX_CONF"
sed -i "s/__API_CENTER__/${API_CENTER_DOMAIN:-api-center.1ex.in}/g" "$NGINX_CONF"
sed -i "s/__API_STAFF__/${API_STAFF_DOMAIN:-api-staff.1ex.in}/g" "$NGINX_CONF"
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/1ex-api
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# PM2
export APP_DIR
pm2 delete 1ex-main 1ex-admin 1ex-center 1ex-staff 2>/dev/null || true
pm2 start deploy/digitalocean/ecosystem.config.cjs
pm2 save
pm2 startup systemd -u root --hp /root 2>/dev/null || true

# Firewall
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH
  ufw allow 'Nginx Full'
  ufw --force enable || true
fi

DROPLET_IP="$(curl -fsSL https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

echo ""
echo "============================================"
echo "  BACKEND READY"
echo "============================================"
echo "  Droplet IP:  $DROPLET_IP"
echo "  PM2:         pm2 status"
echo "  Logs:        pm2 logs 1ex-main"
echo ""
echo "  DNS (Cloudflare/domain panel) — A records:"
echo "    api.1ex.in        -> $DROPLET_IP"
echo "    api-admin.1ex.in  -> $DROPLET_IP"
echo "    api-center.1ex.in -> $DROPLET_IP"
echo "    api-staff.1ex.in  -> $DROPLET_IP"
echo ""
echo "  SSL (after DNS propagates):"
echo "    certbot --nginx -d api.1ex.in -d api-admin.1ex.in -d api-center.1ex.in -d api-staff.1ex.in"
echo ""
echo "  Test:"
echo "    curl -s http://127.0.0.1:1456/ | head"
echo "    curl -s https://api.1ex.in/v1/ -X POST ..."
echo "============================================"
