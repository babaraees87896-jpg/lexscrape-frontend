# Stop 1ex.in stack ports + cloudflared
. (Join-Path $PSScriptRoot "config.ps1")

foreach ($p in @($PORT_MAIN, $PORT_ADMIN, $PORT_CENTER, $PORT_STAFF, $PORT_SCRAPE2)) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procId = $conn.OwningProcess | Select-Object -First 1
        Write-Host "Stopping port $p (PID $procId)"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}
Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Stopping cloudflared PID $($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Write-Host "Done."
