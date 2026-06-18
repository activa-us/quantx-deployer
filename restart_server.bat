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

echo [restart] Waiting for bot runner(s) to shut down cleanly (they watch the server PID) ...
powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(15); do { $r=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '_lp_master\.py' }); if ($r.Count -eq 0) { Write-Host '[restart]   all runners exited cleanly'; break }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); $r=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '_lp_master\.py' }); foreach ($p in $r) { Write-Host ('[restart]   force-killing straggler runner PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Brief pause so Windows releases the port (works non-interactively too).
ping -n 3 127.0.0.1 >nul

echo [restart] Starting QuantX Deployer on http://localhost:%PORT%/ ...
cd /d "%REPO%"
start "QuantX Deployer (port %PORT%)" python -m uvicorn api.main:app --host 0.0.0.0 --port %PORT%

echo [restart] Server launched in its own window. Dashboard: http://localhost:%PORT%/
endlocal
