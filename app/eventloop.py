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
