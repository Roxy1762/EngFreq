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

    # The application uses an in-memory TaskStore that is process-local, so
    # multiple workers would each have their own isolated task map — a task
    # submitted to worker A would not be visible when worker B handles the poll
    # request, causing spurious 404s.  FastAPI + asyncio already handles high
    # concurrency within a single event loop, so one worker is sufficient.
    workers = 1

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=not prod,
        workers=workers,
        log_level="info" if prod else "debug",
    )
