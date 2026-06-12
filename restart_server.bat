@echo off
REM === QuantX Deployer: (re)start the API server ===========================
REM Kills ONLY the process listening on port 8080 (the old uvicorn), then
REM starts a fresh server in its own window. Bot runner processes are NOT
REM touched - they are separate PIDs and don't listen on this port.
REM Note: the server spawns a fresh bot runner on startup and adopts it;
REM this script is safe to double-click or call from a terminal.

setlocal
set PORT=8080
set REPO=%~dp0
REM Options-cache disk cap (GB); code default is 20, set here to be explicit.
set OPTIONS_CACHE_MAX_GB=20

echo [restart] Stopping the process listening on port %PORT% (if any) ...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('[restart]   killing PID ' + $_); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"

echo [restart] Stopping old bot runner(s) so the new server spawns fresh ones ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '_lp_master\.py' } | ForEach-Object { Write-Host ('[restart]   killing runner PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Brief pause so Windows releases the port (works non-interactively too).
ping -n 3 127.0.0.1 >nul

echo [restart] Starting QuantX Deployer on http://localhost:%PORT%/ ...
cd /d "%REPO%"
start "QuantX Deployer (port %PORT%)" python -m uvicorn api.main:app --host 0.0.0.0 --port %PORT%

echo [restart] Server launched in its own window. Dashboard: http://localhost:%PORT%/
endlocal
