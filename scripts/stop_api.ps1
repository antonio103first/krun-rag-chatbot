# Stop / turn off the KRUN RAG FastAPI server.
# Detects first (so the message is honest), tries a graceful /shutdown,
# then force-kills the port listener AND any uvicorn process for this app
# (supervisor + worker) so a parent can't respawn it.
param([int]$Port = 8765)

function Get-KrunPids {
    param([int]$Port)
    $ids = New-Object System.Collections.Generic.HashSet[int]
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        ForEach-Object { [void]$ids.Add([int]$_.OwningProcess) }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*apps.fastapi_server*' } |
        ForEach-Object { [void]$ids.Add([int]$_.ProcessId) }
    return $ids
}

$targets = Get-KrunPids -Port $Port
if ($targets.Count -eq 0) {
    Write-Host "KRUN RAG was not running."
    exit 0
}

# 1) Ask the server to exit cleanly.
try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/shutdown" -TimeoutSec 3 | Out-Null } catch {}
Start-Sleep -Milliseconds 600

# 2) Force-kill anything still alive (re-collect to catch a respawned worker).
foreach ($procId in (Get-KrunPids -Port $Port)) {
    Write-Host "Stopping KRUN RAG PID $procId ..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

Write-Host "KRUN RAG server stopped."
exit 0
