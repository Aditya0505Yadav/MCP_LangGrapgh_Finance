"""One-command launcher: starts the MCP server and the web app, opens the browser.

    python launch.py          (or double-click run.bat on Windows)

Uses live (real-time) data by default. Reads DATABASE_URL, ANTHROPIC_API_KEY and
CLAUDE_MODEL from .env; forces local hostnames for the MCP server and API so it
works outside Docker. Requires PostgreSQL reachable at DATABASE_URL.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

# Local run: real-time data, local hostnames (override any Docker values in .env).
os.environ.setdefault("DATA_MODE", "live")
os.environ["MCP_HOST"] = "127.0.0.1"
os.environ.setdefault("MCP_PORT", "8001")
os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{os.environ['MCP_PORT']}/mcp"
os.environ.setdefault("API_HOST", "127.0.0.1")
os.environ.setdefault("API_PORT", "8000")

PY = sys.executable


def wait_port(host: str, port: int, timeout_s: int = 45) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.5)
    return False


def main() -> int:
    port = int(os.environ["MCP_PORT"])
    api_port = int(os.environ["API_PORT"])
    print(f"[launch] starting MCP server on 127.0.0.1:{port} (data_mode={os.environ['DATA_MODE']})")
    mcp = subprocess.Popen([PY, "-m", "mcp_server.server"], env=os.environ.copy())
    try:
        if not wait_port("127.0.0.1", port):
            print("[launch] ERROR: MCP server did not come up. Check that PostgreSQL is "
                  "running and DATABASE_URL in .env is correct.")
            mcp.terminate()
            return 1
        print("[launch] MCP server is up.")
        url = f"http://localhost:{api_port}"
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
        print(f"[launch] opening {url} — enter a symbol to analyze. Press Ctrl+C to stop.")
        from app.eventloop import run_uvicorn
        run_uvicorn("app.main:app", os.environ["API_HOST"], api_port)
    except KeyboardInterrupt:
        print("\n[launch] shutting down...")
    finally:
        mcp.terminate()
        try:
            mcp.wait(timeout=5)
        except Exception:
            mcp.kill()
        print("[launch] stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
