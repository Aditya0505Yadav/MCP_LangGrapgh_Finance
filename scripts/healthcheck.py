"""Dependency-free TCP health check (used by Docker Compose).

Usage: python scripts/healthcheck.py <port> [host]
Exits 0 if the port accepts a connection, 1 otherwise.
"""

import socket
import sys


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect((host, port))
        return 0
    except OSError as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
