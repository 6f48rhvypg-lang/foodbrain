# FoodBrain icon server -- one-time setup
# Run with: powershell -ExecutionPolicy Bypass -File setup.ps1

$ServiceDir = $PSScriptRoot

Write-Host "==> Creating Python venv ..."
python -m venv "$ServiceDir\.venv"

Write-Host "==> Installing PyTorch ..."
& "$ServiceDir\.venv\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cu128

Write-Host "==> Installing other dependencies ..."
& "$ServiceDir\.venv\Scripts\pip.exe" install diffusers transformers accelerate fastapi uvicorn pillow huggingface_hub

Write-Host "==> Registering Task Scheduler job (FoodbainIconServer) ..."
$Action = New-ScheduledTaskAction `
    -Execute "$ServiceDir\.venv\Scripts\python.exe" `
    -Argument "-m uvicorn icon_server:app --host 0.0.0.0 --port 8188" `
    -WorkingDirectory $ServiceDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName "FoodbainIconServer" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host ""
Write-Host "Done! Starting server now for the first time ..."
Write-Host "(First run downloads ~24 GB of FLUX.1-schnell weights -- go make coffee)"
Write-Host ""
Start-ScheduledTask -TaskName "FoodbainIconServer"
Write-Host "Server starting in background on port 8188."
Write-Host ""
$ip = tailscale ip -4
Write-Host "Your Tailscale IP: $ip"
Write-Host ""
Write-Host "Add this to CT 105 /opt/foodbrain/.env :"
Write-Host "  FOODBRAIN_ICON_LOCAL_URL=http://${ip}:8188"
Write-Host ""
Write-Host "Then on CT 105: cd /opt/foodbrain && git pull && echo FOODBRAIN_ICON_LOCAL_URL=http://${ip}:8188 >> .env && systemctl restart foodbrain"
