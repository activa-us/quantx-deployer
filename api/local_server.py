"""Local QuantX Deployer server entrypoint.

Windows Python 3.11 defaults to the Proactor event loop, which can emit noisy
socket accept errors when browser clients disconnect during polling. For local
Uvicorn runs we switch to the Selector loop before Uvicorn creates the server.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _configure_windows_event_loop() -> None:
    if sys.platform != "win32" or os.environ.get("QX_USE_PROACTOR_LOOP") == "1":
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass


def main() -> None:
    _configure_windows_event_loop()

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the QuantX Deployer API")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    app_dir = str(Path(__file__).resolve().parent.parent)
    reload_excludes = ["bots/*", "logs/*", "*.db", "*.log"] if args.reload else None
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        app_dir=app_dir,
        reload=args.reload,
        reload_excludes=reload_excludes,
    )


if __name__ == "__main__":
    main()
