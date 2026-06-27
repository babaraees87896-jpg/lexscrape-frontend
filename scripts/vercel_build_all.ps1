# Sab frontends Vercel ke liye patch + build
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $Root)

foreach ($t in @("main", "admin", "centerpanel", "staff")) {
    python (Join-Path $Root "vercel_build.py") $t
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host ""
Write-Host "Build complete: vercel-out\"
Write-Host "  main        -> 1ex.in"
Write-Host "  admin       -> admin.1ex.in"
Write-Host "  centerpanel -> center.1ex.in"
Write-Host "  staff       -> staff.1ex.in"
