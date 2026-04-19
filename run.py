"""Entry point: python run.py [--prod]

  --prod   Production mode: disables hot-reload, enables multiple workers
           (default: development mode with auto-reload)
"""
import os
import sys

try:
    from backend.config import settings
    import uvicorn
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing Python dependencies. Run 'start.bat --bootstrap' on Windows "
        "or './start.sh --bootstrap' on Linux/macOS."
    ) from exc


if __name__ == "__main__":
    prod = "--prod" in sys.argv
    workers = 1 if (not prod or os.name == "nt") else 4

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=not prod,
        workers=workers,
        log_level="info" if prod else "debug",
    )
