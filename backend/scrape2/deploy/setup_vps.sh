#!/usr/bin/env bash
# VPS pe ek baar chalao (root ya sudo):
#   cd /var/www/diaapi && sudo bash deploy/setup_vps.sh
set -e

APP_DIR="/var/www/diaapi"
DOMAIN="${DOMAIN:-api.tumhara.com}"

echo "=== DiaAPI VPS Setup (Nginx + PM2) ==="

# System packages
apt update
apt install -y python3 python3-venv python3-pip nginx curl

# Node.js + PM2
if ! command -v pm2 >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt install -y nodejs
  npm install -g pm2
fi

cd "$APP_DIR"

# Python venv + deps
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p output logs
chmod +x start.sh deploy/run_service.sh

# Nginx
cp deploy/nginx-diaapi.conf /etc/nginx/sites-available/diaapi
sed -i "s/api.tumhara.com/${DOMAIN}/g" /etc/nginx/sites-available/diaapi
ln -sf /etc/nginx/sites-available/diaapi /etc/nginx/sites-enabled/diaapi
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# PM2 start
pm2 delete diaapi 2>/dev/null || true
pm2 start deploy/ecosystem.config.cjs
pm2 save
pm2 startup systemd -u root --hp /root 2>/dev/null || pm2 startup

echo ""
echo "DONE!"
echo "  PM2:   pm2 status"
echo "  Logs:  pm2 logs diaapi"
echo "  URL:   http://${DOMAIN}/"
echo "  JSON:  http://${DOMAIN}/all_games.json"
echo ""
echo "SSL: certbot --nginx -d ${DOMAIN}"
