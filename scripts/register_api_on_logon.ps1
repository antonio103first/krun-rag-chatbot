# Register a Windows Scheduled Task that starts the KRUN RAG API server at
# user logon, hidden (no cmd window).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_api_on_logon.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_api_on_logon.ps1 -Unregister
#
# After registration:
#   - Server auto-starts at next logon (and is restartable via the task)
#   - Start it now without logging out:
#       Start-ScheduledTask -TaskName "KrunRagApi_OnLogon"
#   - Stop it:
#       Stop-ScheduledTask -TaskName "KrunRagApi_OnLogon"
#   - Inspect:
#       Get-ScheduledTask -TaskName "KrunRagApi_OnLogon"
#   - Server stays on 127.0.0.1:8765 — verify with: curl http://127.0.0.1:8765/health

param([switch]$Unregister)

$TaskName    = "KrunRagApi_OnLogon"
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Unregistered: $TaskName"
    exit 0
}

# Remove any prior version so this script is idempotent.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# PowerShell -WindowStyle Hidden actually shows a brief flash on logon. To
# fully suppress, route through wscript.exe + a tiny VBScript wrapper. Keep it
# simple here: hidden PowerShell flash is < 1 second and acceptable.
$PsCommand = "Set-Location '$ProjectRoot'; uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$PsCommand`""

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit = PT0S → no time limit (uvicorn runs forever).
# StartWhenAvailable → if PC was off at scheduled time, run on next boot.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Start KRUN RAG FastAPI server at logon (hidden). See scripts/register_api_on_logon.ps1." | Out-Null

Write-Host "Registered: $TaskName"
Write-Host ""
Write-Host "To start the server NOW without logging out:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "To verify it is running after start:"
Write-Host "  curl http://127.0.0.1:8765/health"
