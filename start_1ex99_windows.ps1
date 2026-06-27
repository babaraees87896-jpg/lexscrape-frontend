# 1ex99 Local Stack - Windows (custom ports)
# Main: 1456 | Admin: 1457 | Center Panel: 1458 | Scrape2: 1459 | MongoDB: 27017

param(
    [switch]$Silent,
    [switch]$AutoStart
)

$ErrorActionPreference = "Continue"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$BACKEND = Join-Path $ROOT "backend"
$FRONTEND = Join-Path $ROOT "frontend"
$BLUEWIN = Join-Path $BACKEND "bluewin"
$SCRAPE2 = Join-Path $BACKEND "scrape2"
$LOG = Join-Path $ROOT "logs"
$MONGO_DATA = Join-Path $ROOT ".mongo-data"
$MONGOD = "C:\Program Files\MongoDB\Server\8.3\bin\mongod.exe"

if ($Silent) {
    New-Item -ItemType Directory -Force -Path $LOG | Out-Null
    $autoLog = Join-Path $LOG "autostart.log"
    "`n==== Auto-start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File -Append $autoLog -Encoding utf8
    Start-Transcript -Path $autoLog -Append -ErrorAction SilentlyContinue | Out-Null
}

$PORT_MAIN = 1456
$PORT_ADMIN = 1457
$PORT_CENTER = 1458
$PORT_STAFF = 1460
$PORT_SCRAPE2 = 1459
$PORT_MONGO = 27017
$ENABLE_SCRAPE2 = $false   # scrape2 poll + serve - set $true to enable

# WNP9 staff API - auto-declare exact results (working/secrets/wnp9.json)
$WNP9_USERNAME = "OW1000"
$WNP9_PASSWORD = ""
$WNP9_API_BASE = "https://api.wnp9.pro/v1/"
$wnp9SecretsFile = Join-Path $ROOT "secrets\wnp9.json"
if (Test-Path $wnp9SecretsFile) {
    try {
        $wnp9Secrets = Get-Content $wnp9SecretsFile -Raw | ConvertFrom-Json
        if ($wnp9Secrets.username) { $WNP9_USERNAME = [string]$wnp9Secrets.username }
        if ($wnp9Secrets.password) { $WNP9_PASSWORD = [string]$wnp9Secrets.password }
        if ($wnp9Secrets.apiBase) { $WNP9_API_BASE = [string]$wnp9Secrets.apiBase }
    } catch {
        Write-Host "  WARN: could not read $wnp9SecretsFile - $_"
    }
}
if ([string]::IsNullOrWhiteSpace($WNP9_PASSWORD)) {
    Write-Host '  WARN: WNP9_PASSWORD empty - edit working\secrets\wnp9.json'
}

$Wnp9Env = @{
    WNP9_USERNAME = $WNP9_USERNAME
    WNP9_PASSWORD = $WNP9_PASSWORD
    WNP9_API_BASE = $WNP9_API_BASE
    WNP9_SECRETS_FILE = $wnp9SecretsFile
}

New-Item -ItemType Directory -Force -Path $LOG, $MONGO_DATA, (Join-Path $SCRAPE2 "output\sport\scorecard") | Out-Null

Write-Host "============================================"
Write-Host "  Starting 1ex99 Local Stack (Windows)"
Write-Host "  Main: $PORT_MAIN | Admin: $PORT_ADMIN | Center: $PORT_CENTER | Staff: $PORT_STAFF | Scrape2: $PORT_SCRAPE2"
Write-Host "============================================"

function Stop-OrphanStackPython {
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
            Write-Host "  Stopping orphan python PID $($_.ProcessId)..."
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

function Stop-StackPorts {
    foreach ($p in @($PORT_MAIN, $PORT_ADMIN, $PORT_CENTER, $PORT_STAFF, $PORT_SCRAPE2, 8888, 8889, 8891, 8900)) {
        Stop-PortProcess $p
    }
}

function Wait-ForHttpPort {
    param([int]$Port, [int]$MaxSeconds = 40)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening $Port) {
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 4
                if ($r.StatusCode -ge 200) { return $true }
            } catch {}
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-CloudflaredIfReady {
    if (-not (Wait-ForHttpPort -Port $PORT_MAIN -MaxSeconds 5)) {
        Write-Host "  ERROR: Main site not up on $PORT_MAIN - skipping cloudflared (prevents 502 on 1ex.in)"
        return $false
    }
    Write-Host "  Stopping old cloudflared tunnels..."
    Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "    stopping PID $($_.Id)"
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 8
    Write-Host "  Starting cloudflared tunnel (1ex-live)..."
    Start-Process -FilePath "cloudflared" `
        -ArgumentList "tunnel", "--config", "$env:USERPROFILE\.cloudflared\config.yml", "run", "1ex-live" `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LOG "cloudflared.log") `
        -RedirectStandardError (Join-Path $LOG "cloudflared.err.log")
    Start-Sleep -Seconds 6
    if (Get-Process cloudflared -ErrorAction SilentlyContinue) {
        Write-Host "  cloudflared running"
        return $true
    }
    Write-Host "  WARN: cloudflared did not stay up - check logs\cloudflared.err.log"
    return $false
}

function Wait-ForMongo {
    param([int]$MaxSeconds = 45)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening $PORT_MONGO) {
            try {
                $ping = python -c "from pymongo import MongoClient; MongoClient('mongodb://127.0.0.1:27017',serverSelectionTimeoutMS=3000).admin.command('ping'); print('ok')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ping -eq "ok") { return $true }
            } catch {}
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Invoke-MongoRestore {
    param([string]$ScriptPath, [string]$LogPath, [int]$Retries = 4)
    for ($i = 1; $i -le $Retries; $i++) {
        if (-not (Wait-ForMongo -MaxSeconds 20)) {
            Write-Host "  MongoDB not ready (attempt $i/$Retries)..."
            Start-Sleep -Seconds 3
            continue
        }
        python $ScriptPath 2>&1 | Tee-Object -FilePath $LogPath
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Host "  Restore/import failed (attempt $i/$Retries), retrying..."
        Start-Sleep -Seconds ([Math]::Min(8, 2 * $i))
    }
    return $false
}

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
    $safeName = ($Name -replace '[^a-zA-Z0-9_-]', '_')
    $cmdFile = Join-Path $env:TEMP ("ex99-{0}-{1}.cmd" -f $safeName, $PID)
    $lines = New-Object System.Collections.Generic.List[string]
    [void]$lines.Add('@echo off')
    [void]$lines.Add(('cd /d "{0}"' -f $WorkDir))
    foreach ($key in ($EnvVars.Keys | Sort-Object)) {
        $val = [string]$EnvVars[$key]
        $val = $val -replace '%', '%%'
        [void]$lines.Add(('set {0}={1}' -f $key, $val))
    }
    [void]$lines.Add(("python {0}" -f $Script))
    Set-Content -Path $cmdFile -Value $lines.ToArray() -Encoding ASCII
    Start-Process -FilePath $cmdFile `
        -WorkingDirectory $WorkDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError ($LogFile + ".err")
    Write-Host "  Started $Name (log: $LogFile)"
}

function Merge-EnvHashtable {
    param([hashtable]$Base)
    $merged = @{}
    foreach ($key in $Wnp9Env.Keys) { $merged[$key] = $Wnp9Env[$key] }
    foreach ($key in $Base.Keys) { $merged[$key] = $Base[$key] }
    return $merged
}

if ($AutoStart) {
    $alreadyUp = Get-NetTCPConnection -LocalPort $PORT_MAIN -State Listen -ErrorAction SilentlyContinue
    $cfUp = Get-Process cloudflared -ErrorAction SilentlyContinue
    if ($alreadyUp -and $cfUp) {
        Write-Host "  AutoStart: stack already running (port $PORT_MAIN + cloudflared) - skip"
        if ($Silent) { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null }
        exit 0
    }
}

Stop-StackPorts
Stop-OrphanStackPython

# --- MongoDB ---
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
    Write-Host "  Trying MongoDB Windows service..."
    Start-Service MongoDB -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

# --- MongoDB data: pehle se .mongo-data hai to touch mat karo; warna saved backup restore ---
$checkScript = Join-Path $ROOT "check_mongo_users.py"
@"
from pymongo import MongoClient
c = MongoClient('mongodb://localhost:27017', serverSelectionTimeoutMS=8000)
print(c['ex99_local']['users'].count_documents({}))
"@ | Set-Content -Path $checkScript -Encoding UTF8

$dbFilesExist = (Test-Path (Join-Path $MONGO_DATA "WiredTiger")) -or (Test-Path (Join-Path $MONGO_DATA "storage.bson"))
$savedBackup = Join-Path $ROOT "saved_state\mongo_backup.json"

$userCount = python $checkScript 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  MongoDB not reachable yet, waiting..."
    if (-not (Wait-ForMongo -MaxSeconds 45)) {
        Write-Host "  ERROR: MongoDB did not become ready on port $PORT_MONGO"
        Write-Host "  Try: .\stop_1ex99_windows.bat then start again"
    } else {
        $userCount = python $checkScript 2>$null
    }
}

if ($dbFilesExist) {
    Write-Host "  MongoDB data preserved ($MONGO_DATA) - skip import"
} elseif (Test-Path $savedBackup) {
    Write-Host "  Restoring MongoDB from saved_state backup..."
    if (-not (Invoke-MongoRestore -ScriptPath (Join-Path $ROOT "restore_mongodb_backup.py") -LogPath (Join-Path $LOG "restore.log"))) {
        Write-Host "  WARN: MongoDB restore failed - stack will start but DB may be empty"
    }
} elseif ([string]::IsNullOrWhiteSpace($userCount) -or $userCount -eq "0") {
    Write-Host "  First-time import from mongodb_full_data.json..."
    if (-not (Invoke-MongoRestore -ScriptPath (Join-Path $ROOT "import_mongodb_data.py") -LogPath (Join-Path $LOG "import.log"))) {
        Write-Host "  WARN: MongoDB import failed - stack will start but DB may be empty"
    }
} else {
    Write-Host "  MongoDB already has data - skip import"
}

# --- Main site ---
Stop-PortProcess $PORT_MAIN
Start-PythonService -Name "main site" -WorkDir $BACKEND -Script "serve_local.py" `
    -LogFile (Join-Path $LOG "main.log") -EnvVars (Merge-EnvHashtable @{
        EX99_PORT = $PORT_MAIN
        EX99_HOST = "1ex.in"
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        EX99_LOCAL_ONLY = "1"
        EX99_ADMIN_UPSTREAM_PORT = $PORT_ADMIN
        EX99_ADMIN_UPSTREAM_HOST = "127.0.0.1"
        EX99_USE_LOCAL_SCORECARD = "0"
        EX99_SCORECARD_UI_VER = "ex99sc11"
        EX99_SCORECARD_REFRESH_SEC = "1"
        EX99_SCORECARD_LIVE_HUB = "1"
        EX99_SCORECARD_PREWARM = "1"
        SCORECARD_HTTP_LIVE = "0"
        SCORECARD_LIVE_MAX_AGE = "1"
        EX99_AUTO_DECISION = "1"
        EX99_MAX_HTTP_THREADS = "200"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    })
if (-not (Wait-ForHttpPort -Port $PORT_MAIN -MaxSeconds 30)) {
    Write-Host "  ERROR: Main site failed to start on port $PORT_MAIN - check logs\main.log.err"
} else {
    Write-Host "  Main site ready on http://127.0.0.1:$PORT_MAIN/"
}
Start-Sleep -Seconds 2

# --- Admin panel ---
Stop-PortProcess $PORT_ADMIN
Start-PythonService -Name "admin panel" -WorkDir $BACKEND -Script "serve_admin.py" `
    -LogFile (Join-Path $LOG "admin.log") -EnvVars @{
        EX99_ADMIN_PORT = $PORT_ADMIN
        EX99_ADMIN_HOST = "admin.1ex.in"
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 3

# --- Center / Operating panel ---
Stop-PortProcess $PORT_CENTER
Start-PythonService -Name "center/operating panel" -WorkDir $BACKEND -Script "serve_centerpanel.py" `
    -LogFile (Join-Path $LOG "centerpanel.log") -EnvVars @{
        EX99_CENTERPANEL_PORT = $PORT_CENTER
        EX99_CENTERPANEL_HOST = "center.1ex.in"
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    }
Start-Sleep -Seconds 3

# --- Staff panel (BlueWin - NOT center panel) ---
Stop-PortProcess $PORT_STAFF
Start-PythonService -Name "staff panel (BlueWin)" -WorkDir $BLUEWIN -Script "serve_bluewin.py" `
    -LogFile (Join-Path $LOG "staff.log") -EnvVars (Merge-EnvHashtable @{
        BLUEWIN_PORT = $PORT_STAFF
        BLUEWIN_PUBLIC_HOST = "staff.1ex.in"
        STAFF_HOST = "staff.1ex.in"
        EX99_MONGO_DB = "ex99_local"
        EX99_MONGO_URI = "mongodb://localhost:27017"
        PYTHONUNBUFFERED = "1"
        PYTHONIOENCODING = "utf-8"
    })
Start-Sleep -Seconds 3

# --- Scrape2 (optional) ---
Stop-PortProcess $PORT_SCRAPE2
if ($ENABLE_SCRAPE2) {
    $cookiesFile = Join-Path $SCRAPE2 "cookies.txt"
    if (-not (Test-Path $cookiesFile)) {
        Push-Location $SCRAPE2
        cmd /c "set LOGIN_USER=Demo9304 && set LOGIN_PASS=Demo1234 && python api_login.py --username Demo9304 --password Demo1234" 2>$null
        Pop-Location
    }

    Start-PythonService -Name "scrape2 poll" -WorkDir $SCRAPE2 -Script "poll_sport.py" `
        -LogFile (Join-Path $LOG "scrape2-poll.log") -EnvVars @{
            SPORT_SCORECARD = "1"
            SPORT_MATCH_ALL = "1"
            SPORT_POLL_INTERVAL = "20"
            SCORECARD_TIMEOUT = "1.5"
            DG_ENFORCE_IP = "0"
            PYTHONUNBUFFERED = "1"
            PYTHONIOENCODING = "utf-8"
        }
    Start-Sleep -Seconds 2

    Start-PythonService -Name "scrape2 serve" -WorkDir $SCRAPE2 -Script "serve.py --port $PORT_SCRAPE2 --directory output" `
        -LogFile (Join-Path $LOG "scrape2-serve.log") -EnvVars @{
            PORT = $PORT_SCRAPE2
            DG_ENFORCE_IP = "0"
            PYTHONUNBUFFERED = "1"
            PYTHONIOENCODING = "utf-8"
        }
    Start-Sleep -Seconds 3
} else {
    Write-Host "  Scrape2 disabled (ENABLE_SCRAPE2=false)"
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match 'poll_sport\.py'
    } | ForEach-Object {
        Write-Host "    stopping poll_sport PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# --- Cloudflare tunnel (1ex-live) — sirf jab main site 1456 par up ho ---
Start-CloudflaredIfReady | Out-Null

# --- Status ---
Write-Host ""
Write-Host "=== Status ==="
foreach ($p in @($PORT_MONGO, $PORT_SCRAPE2, $PORT_MAIN, $PORT_ADMIN, $PORT_CENTER, $PORT_STAFF)) {
    if (Test-PortListening $p) {
        Write-Host "  port $p : UP"
    } else {
        Write-Host "  port $p : DOWN"
    }
}
if (-not (Test-PortListening $PORT_MAIN)) {
    Write-Host ""
    Write-Host "  *** 1ex.in will show 502 until port $PORT_MAIN is UP ***"
    Write-Host "  *** Purana Downloads copy (8888) band karo: stop_1ex99_windows.bat ***"
}

Write-Host ""
Write-Host "=== Local URLs ==="
Write-Host "  Main site:     http://127.0.0.1:$PORT_MAIN/"
Write-Host "  Admin panel:   http://127.0.0.1:$PORT_ADMIN/"
Write-Host "  Center/Operating: http://127.0.0.1:$PORT_CENTER/"
Write-Host "  Staff (BlueWin): http://127.0.0.1:$PORT_STAFF/"
Write-Host "  Scrape2:       http://127.0.0.1:$PORT_SCRAPE2/sport/scorecard/"
Write-Host ""
Write-Host "=== Live URLs (Cloudflare tunnel) ==="
Write-Host "  https://1ex.in/"
Write-Host "  https://www.1ex.in/"
Write-Host "  https://admin.1ex.in/       (Admin panel)"
Write-Host "  https://center.1ex.in/      (Operating/Center panel)"
Write-Host "  https://staff.1ex.in/       (BlueWin Staff panel)"
Write-Host ""
Write-Host "  Client login: C324001 / 123456"
Write-Host "  Admin:        OWNER001 / admin@123"
Write-Host "  Center:       ADMIN001 / admin@123"
Write-Host "  Staff BlueWin: OW1000 / Bluewin@4923"
Write-Host ('  WNP9 API:      {0} (secrets\wnp9.json)' -f $WNP9_USERNAME)
Write-Host ""
Write-Host "  Logs folder: $LOG"

if ($Silent) {
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}
