# Upload MongoDB backup from Windows to DigitalOcean droplet
# Usage: .\deploy\digitalocean\upload-backup.ps1 -DropletIP 1.2.3.4

param(
    [Parameter(Mandatory = $true)]
    [string]$DropletIP,
    [string]$User = "root",
    [string]$Backup = "saved_state\mongo_backup.json"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Local = Join-Path $Root $Backup

if (-not (Test-Path $Local)) {
    Write-Error "Backup not found: $Local"
}

Write-Host "Uploading $Local -> ${User}@${DropletIP}:/opt/1ex/saved_state/"
ssh "${User}@${DropletIP}" "mkdir -p /opt/1ex/saved_state"
scp $Local "${User}@${DropletIP}:/opt/1ex/saved_state/mongo_backup.json"
ssh "${User}@${DropletIP}" "cd /opt/1ex && .venv/bin/python restore_mongodb_backup.py"
Write-Host "Done."
