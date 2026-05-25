#!/usr/bin/env python3
"""QuantX Deployer -- start the app.

Architecture:
  Backtesting = Railway (remote) if CENTRAL_API_URL is set, else local (Yahoo)
  Trading     = always local (your IBKR / LongPort)
"""
import os
import sys
import subprocess
import webbrowser
import time
from pathlib import Path

BASE = Path(__file__).parent


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env()
    port = int(os.environ.get("PORT", 8080))
    central = os.environ.get("CENTRAL_API_URL", "")
    is_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    db_url = os.environ.get("DATABASE_URL", "")
    auth_mode = "postgres" if db_url else "local (SQLite)"

    print("=" * 50)
    print("  QuantX Deployer v1.2")
    if is_railway:
        print("  Hosting: Railway (web mode)")
    else:
        print(f"  App: http://localhost:{port}")
    print(f"  Auth mode: {auth_mode}")
    if central:
        print(f"  Backtest server: {central}")
    else:
        print("  Backtest server: local (Yahoo Finance)")
    print("  Trading: local (your IBKR / LongPort)")
    print("=" * 50)

    # On Railway we bind 0.0.0.0 and skip browser + subprocess wrapping.
    # Locally we keep the subprocess + auto-open-browser flow for UX.
    dev_mode = os.environ.get("DEV_MODE", "0") == "1"
    python = sys.executable
    host = "0.0.0.0" if is_railway else "127.0.0.1"
    cmd = [python, "-m", "uvicorn", "api.main:app",
           "--host", host, f"--port={port}"]
    if is_railway:
        # Railway sends SIGTERM for graceful shutdown; single worker avoids
        # duplicated background-prewarm threads hammering R2.
        cmd += ["--workers", "1"]
    if dev_mode and not is_railway:
        cmd += ["--reload",
                "--reload-exclude=bots/*", "--reload-exclude=logs/*",
                "--reload-exclude=*.db", "--reload-exclude=*.log"]
        print("  Mode: DEVELOPMENT (auto-reload ON)")
    else:
        print("  Mode: PRODUCTION (auto-reload OFF)")

    proc = subprocess.Popen(cmd, cwd=str(BASE))

    if not is_railway:
        time.sleep(2)
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass  # headless local server still works

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proc.terminate()


if __name__ == "__main__":
    main()
