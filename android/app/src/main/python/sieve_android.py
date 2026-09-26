"""Runs Sieve inside the Android app.

MainActivity and SieveService call `start(data_dir, port)` through Chaquopy.
The server listens on 127.0.0.1 only: nothing else on the network can reach
it. It runs on its own thread for as long as the app's process lives; the
foreground service keeps that process alive while syncs and downloads run.
"""

from __future__ import annotations

import threading
import time
import urllib.request

_server = None
_thread = None
_lock = threading.Lock()


def start(data_dir: str, port: int = 8377) -> str:
    """Start the server once; later calls return the same address."""
    global _server, _thread
    url = f"http://127.0.0.1:{port}"
    with _lock:
        if _thread is not None and _thread.is_alive():
            return url
        import uvicorn

        from sieve.app import create_app
        from sieve.config import Config

        cfg = Config(data_dir=data_dir)
        app = create_app(cfg, start_worker=True)

        class Server(uvicorn.Server):
            def install_signal_handlers(self) -> None:   # not the main thread on Android
                pass

        _server = Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                        loop="asyncio", http="h11", lifespan="on"))
        _thread = threading.Thread(target=_server.run, name="sieve-server", daemon=True)
        _thread.start()
    wait_until_ready(url)
    return url


def wait_until_ready(url: str, seconds: float = 45.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def stop() -> None:
    if _server is not None:
        _server.should_exit = True
