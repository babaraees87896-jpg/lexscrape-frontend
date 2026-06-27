# 1ex.in deployment — shared paths & domain hosts
$ErrorActionPreference = "Continue"

$DEPLOY_ROOT = $PSScriptRoot
$WORKING_ROOT = Split-Path -Parent $DEPLOY_ROOT

$BACKEND = Join-Path $WORKING_ROOT "backend"
$FRONTEND = Join-Path $WORKING_ROOT "frontend"
$BLUEWIN = Join-Path $BACKEND "bluewin"
$SCRAPE2 = Join-Path $BACKEND "scrape2"
$LOG = Join-Path $DEPLOY_ROOT "logs"
$MONGO_DATA = Join-Path $WORKING_ROOT ".mongo-data"
$MONGOD = "C:\Program Files\MongoDB\Server\8.3\bin\mongod.exe"
$SAVED_BACKUP = Join-Path $WORKING_ROOT "saved_state\mongo_backup.json"

$PORT_MAIN = 1456
$PORT_ADMIN = 1457
$PORT_CENTER = 1458
$PORT_STAFF = 1460
$PORT_SCRAPE2 = 1459
$PORT_MONGO = 27017
$ENABLE_SCRAPE2 = $false

$MAIN_HOST = "1ex.in"
$ADMIN_HOST = "admin.1ex.in"
$CENTER_HOST = "center.1ex.in"
$STAFF_HOST = "staff.1ex.in"

$TUNNEL_NAME = "1ex-live"
$CLOUDFLARED_CONFIG = Join-Path $DEPLOY_ROOT "cloudflared.yml"
