"""Start the Ledgerly mock app in a background thread (used by tests, benchmark and --start-server)."""
from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int | None = None) -> tuple[str, uvicorn.Server]:
    from app.server import app

    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/api/invoices", timeout=0.5)
            return url, server
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("Ledgerly server did not start")
