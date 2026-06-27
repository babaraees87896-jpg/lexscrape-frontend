# 1ex.in — start full stack (main site + admin + center + staff + tunnel)
. (Join-Path $PSScriptRoot "config.ps1")

New-Item -ItemType Directory -Force -Path $LOG, $MONGO_DATA, (Join-Path $SCRAPE2 "output\sport\scorecard") | Out-Null

Write-Host "============================================"
Write-Host "  1ex.in Stack"
Write-Host "  Main: $PORT_MAIN | Admin: $PORT_ADMIN | Center: $PORT_CENTER | Staff: $PORT_STAFF"
Write-Host "  Folder: $DEPLOY_ROOT"
Write-Host "============================================"

function Test-PortListening([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Stop-PortProcess([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procId = $conn.OwningProcess | Select-Object -First 1
        if ($procId) {
            Write-Host "  Stopping process on port $Port (PID $procId)..."
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }
}

function Start-PythonService {
    param(
        [string]$Name,
        [string]$WorkDir,
        [string]$Script,
        [string]$LogFile,
        [hashtable]$EnvVars
    )
    $envBlock = ""
    foreach ($key in $EnvVars.Keys) {
        $val = $EnvVars[$key]
        $envBlock += "set `"$key=$val`" && "
    }
    $cmd = "cd /d `"$WorkDir`" && $envBlock python $Script"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd `
        -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError ($LogFile + ".err")
    Write-Host "  Started $Name (log: $LogFile)"
}

# MongoDB
if (Test-PortListening $PORT_MONGO) {
    Write-Host "  MongoDB already running on $PORT_MONGO"
} elseif (Test-Path $MONGOD) {
    Write-Host "  Starting MongoDB on $PORT_MONGO..."
    Start-Process -FilePath $MONGOD `
        -ArgumentList "--dbpath", $MONGO_DATA, "--bind_ip", "127.0.0.1", "--port", "$PORT_MONGO" `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LOG "mongodb.log") `
        -RedirectStandardError (Join-Path $LOG "mongodb.err.log")
    Start-Sleep -Seconds 4
} else {
    Start-Service MongoDB -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

python (Join-Path $DEPLOY_ROOT "add_domain.py") 2>$null | Write-Host

# Main
Stop-PortProcess $PORT_MAIN
Start-PythonService -Name "1ex.in main" -WorkDir $BACKEND -Script "serve_local.py" `
    -LogFile (Join-Path $LOG "main.log") -EnvVars @{
        EX99_PORT = $PORT_MAIN
        EX99_HOST = $MAIN_HOST
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        EX99_LOCAL_ONLY = "1"
        EX99_ADMIN_UPSTREAM_PORT = $PORT_ADMIN
        EX99_ADMIN_UPSTREAM_HOST = "127.0.0.1"
        EX99_SCORECARD_UI_VER = "ex99sc10"
        EX99_SCORECARD_REFRESH_SEC = "5"
        EX99_SCORECARD_PREWARM = "1"
        SCORECARD_HTTP_LIVE = "0"
        SCORECARD_LIVE_MAX_AGE = "2"
        EX99_AUTO_DECISION = "1"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 4

# Admin
Stop-PortProcess $PORT_ADMIN
Start-PythonService -Name "admin.1ex.in" -WorkDir $BACKEND -Script "serve_admin.py" `
    -LogFile (Join-Path $LOG "admin.log") -EnvVars @{
        EX99_ADMIN_PORT = $PORT_ADMIN
        EX99_ADMIN_HOST = $ADMIN_HOST
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 3

# Center
Stop-PortProcess $PORT_CENTER
Start-PythonService -Name "center.1ex.in" -WorkDir $BACKEND -Script "serve_centerpanel.py" `
    -LogFile (Join-Path $LOG "centerpanel.log") -EnvVars @{
        EX99_CENTERPANEL_PORT = $PORT_CENTER
        EX99_CENTERPANEL_HOST = $CENTER_HOST
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 3

# Staff
Stop-PortProcess $PORT_STAFF
Start-PythonService -Name "staff.1ex.in" -WorkDir $BLUEWIN -Script "serve_bluewin.py" `
    -LogFile (Join-Path $LOG "staff.log") -EnvVars @{
        BLUEWIN_PORT = $PORT_STAFF
        BLUEWIN_PUBLIC_HOST = $STAFF_HOST
        STAFF_HOST = $STAFF_HOST
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 3

# Tunnel
Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
if (Test-Path $CLOUDFLARED_CONFIG) {
    Write-Host "  Starting cloudflared ($TUNNEL_NAME) with $CLOUDFLARED_CONFIG"
    Start-Process -FilePath "cloudflared" `
        -ArgumentList "tunnel", "--config", $CLOUDFLARED_CONFIG, "run", $TUNNEL_NAME `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LOG "cloudflared.log") `
        -RedirectStandardError (Join-Path $LOG "cloudflared.err.log")
} else {
    Write-Host "  WARN: cloudflared.yml missing in $DEPLOY_ROOT"
}
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "=== 1ex.in URLs ==="
Write-Host "  https://1ex.in/login"
Write-Host "  https://admin.1ex.in/"
Write-Host "  https://center.1ex.in/"
Write-Host "  https://staff.1ex.in/"
Write-Host "  Logs: $LOG"
