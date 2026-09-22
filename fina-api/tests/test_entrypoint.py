import asyncio
import signal
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
import uvicorn
from dishka import AsyncContainer
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

import fina.__main__ as entrypoint
import fina.lifespan as lifecycle
from fina.config import Settings
from fina.domain.clock import Clock, SystemClock
from fina.runtime import RuntimeState


@pytest.mark.parametrize("trigger", ["signal", "server_exit"])
def test_shutdown_stops_workers_before_waiting_for_http(
    monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    async def scenario() -> None:
        runtime = RuntimeState()
        container = AsyncMock()
        dependencies: dict[type[object], object] = {
            RuntimeState: runtime,
            Clock: SystemClock(),
            AsyncEngine: object(),
        }

        def _get_dependency(kind: type[object]) -> object:
            return dependencies[kind]

        container.get.side_effect = _get_dependency
        workers_stopped = asyncio.Event()
        worker_count = 0

        class Worker:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def run_forever(self, stop_event: asyncio.Event) -> None:
                nonlocal worker_count
                await stop_event.wait()
                worker_count += 1
                if worker_count == 2:
                    workers_stopped.set()

        started = asyncio.Event()
        http_draining = asyncio.Event()
        release_http = asyncio.Event()
        exit_server = asyncio.Event()
        configs: list[uvicorn.Config] = []

        class Server:
            def __init__(self, config: uvicorn.Config) -> None:
                self.config = config
                self.should_exit = False
                configs.append(config)

            async def serve(self) -> None:
                if self.config.port == entrypoint.ROUTES_PORT:
                    started.set()
                    if trigger == "server_exit":
                        await exit_server.wait()
                        return
                while not self.should_exit:
                    await asyncio.sleep(0.001)
                if self.config.port == entrypoint.ROUTES_PORT:
                    http_draining.set()
                    await release_http.wait()

        settings = Settings(
            database_url="postgresql+asyncpg://unused/db", mcp_api_key=SecretStr("test")
        )
        monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)

        async def fake_resolve_audio_adapters(
            settings: Settings, container: AsyncContainer
        ) -> tuple[object, None]:
            return object(), None

        monkeypatch.setattr(entrypoint, "resolve_audio_adapters", fake_resolve_audio_adapters)

        def _build_container(settings: Settings) -> AsyncMock:
            return container

        def _build_app(container: AsyncMock) -> FastAPI:
            return FastAPI()

        monkeypatch.setattr(entrypoint, "build_container", _build_container)
        monkeypatch.setattr(entrypoint, "_build_routes_app", _build_app)
        monkeypatch.setattr(entrypoint, "_build_management_app", _build_app)
        monkeypatch.setattr(entrypoint, "_CoordinatedServer", Server)
        monkeypatch.setattr(lifecycle, "DiscoveryWorker", Worker)
        monkeypatch.setattr(lifecycle, "FetchWorker", Worker)
        handlers: dict[int, Callable[[], None]] = {}
        loop = asyncio.get_running_loop()

        def _add_signal_handler(sig: int, cb: Callable[[], None]) -> None:
            handlers[sig] = cb

        def _remove_signal_handler(sig: int) -> None:
            handlers.pop(sig, None)

        monkeypatch.setattr(loop, "add_signal_handler", _add_signal_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", _remove_signal_handler)

        task = asyncio.create_task(entrypoint.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert runtime.started
            if trigger == "signal":
                handlers[signal.SIGTERM]()
                await asyncio.wait_for(http_draining.wait(), timeout=1)
            else:
                exit_server.set()
            await asyncio.wait_for(workers_stopped.wait(), timeout=1)
            assert runtime.draining
            assert not runtime.started
            if trigger == "signal":
                assert not task.done(), "HTTP should still be draining while workers stop"
                container.close.assert_not_awaited()
            assert len(configs) == 2
            assert all(
                config.timeout_graceful_shutdown is not None
                and 0 < config.timeout_graceful_shutdown <= 5
                for config in configs
            )
            assert all(config.log_config is None and not config.access_log for config in configs)
        finally:
            release_http.set()
            exit_server.set()
            await asyncio.wait_for(task, timeout=1)
        container.close.assert_awaited_once()
        assert not handlers

    asyncio.run(scenario())
