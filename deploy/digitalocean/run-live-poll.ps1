# Run live poll setup on DigitalOcean (will prompt for root password once)
param(
    [string]$DropletIP = "165.245.188.51",
    [string]$AppDir = "/opt/lex"
)

$ErrorActionPreference = "Stop"
$cmd = "curl -fsSL 'https://raw.githubusercontent.com/babaraees87896-jpg/lexscrape-frontend/main/deploy/digitalocean/remote-live-poll.sh' | APP_DIR=$AppDir bash"

Write-Host "Connecting to root@${DropletIP} ..."
Write-Host "Enter droplet password when prompted."
Write-Host ""

ssh -o StrictHostKeyChecking=accept-new "root@${DropletIP}" $cmd
