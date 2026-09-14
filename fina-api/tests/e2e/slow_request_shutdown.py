"""Run inside the production image to exercise SIGTERM with an active request."""

import asyncio
import os
import signal
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

from fastapi import FastAPI

import fina.__main__ as entrypoint

request_started = threading.Event()
request_cancelled = threading.Event()
signalled_at = []
port = int(sys.argv[1])


def build_slow_app(container):
    app = FastAPI()

    @app.get("/slow")
    async def slow():
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    return app


def request():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/slow", timeout=15) as response:
                response.read()
            return
        except URLError:
            if request_started.is_set():
                return
            time.sleep(0.05)


def stop_with_active_request():
    if request_started.wait(timeout=10):
        signalled_at.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGTERM)


if port == entrypoint.ROUTES_PORT:
    entrypoint._build_routes_app = build_slow_app
else:
    entrypoint._build_management_app = build_slow_app

# Even a regression must terminate this throwaway container and let --rm clean it up.
signal.alarm(20)
threading.Thread(target=request, daemon=True).start()
threading.Thread(target=stop_with_active_request, daemon=True).start()
entrypoint.main()
assert request_started.is_set()
assert request_cancelled.is_set()
assert time.monotonic() - signalled_at[0] < 10
print("active request cancelled; shutdown completed")
