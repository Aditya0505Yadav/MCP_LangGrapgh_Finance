"""Windows event-loop policy fix for psycopg's async mode.

psycopg's async connections cannot run on Windows' default ``ProactorEventLoop``;
they require the ``SelectorEventLoop``. Every process entry point (MCP server,
API, CLI) calls this before an event loop is created. No-op off Windows (Linux /
Docker already use a selector-based loop).
"""

from __future__ import annotations

import asyncio
import sys


def use_selector_event_loop_on_windows() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def run_uvicorn(app: str, host: str, port: int, log_level: str = "info") -> None:
    """Serve a FastAPI app under a Selector event loop on Windows.

    uvicorn.run() installs its own loop (ProactorEventLoop on Windows), which
    psycopg's async mode cannot use. Running the server via asyncio.run after
    setting the Selector policy keeps our loop and avoids that override.
    """
    import asyncio as _asyncio

    import uvicorn

    use_selector_event_loop_on_windows()
    config = uvicorn.Config(app, host=host, port=port, loop="asyncio", log_level=log_level)
    _asyncio.run(uvicorn.Server(config).serve())
