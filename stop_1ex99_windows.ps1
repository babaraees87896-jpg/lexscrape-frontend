# Stop 1ex99 local stack — ports + stale python (Downloads copy bhi)
$ErrorActionPreference = "Continue"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$PORT_MAIN = 1456
$PORT_ADMIN = 1457
$PORT_CENTER = 1458
$PORT_STAFF = 1460
$PORT_SCRAPE2 = 1459

Write-Host "Stopping 1ex99 stack..."

function Stop-PortProcess([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procId = $conn.OwningProcess | Select-Object -First 1
        if ($procId) {
            Write-Host "  Port $Port -> PID $procId"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}

foreach ($p in @($PORT_MAIN, $PORT_ADMIN, $PORT_CENTER, $PORT_STAFF, $PORT_SCRAPE2, 8888, 8889, 8891, 8900)) {
    Stop-PortProcess $p
}

$patterns = @(
    "serve_local.py", "serve_admin.py", "serve_centerpanel.py",
    "serve_bluewin.py", "serve_staff.py", "ex99_poll.py"
)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    $cmd = [string]$_.CommandLine
    $hit = ($cmd -like "*$ROOT*")
    if (-not $hit) {
        foreach ($pat in $patterns) {
            if ($cmd -like "*$pat*") { $hit = $true; break }
        }
    }
    if ($hit) {
        Write-Host "  Python PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 2
Write-Host "Done. MongoDB (port 27017) left running if already up."
