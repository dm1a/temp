import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import httpx

ROOT = Path(__file__).parents[2]
API_KEY = "e2e-only-api-key"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}
SERVICES = ("api-1", "api-2")

# docker/compose.e2e.public.yaml needs a public path to Docker Hub/ghcr.io
# (docker/Dockerfile.public, plain postgres:15-alpine). On a machine with no
# public internet access, set this to docker/compose.e2e.yaml, which builds
# docker/Dockerfile's runtime stage and pulls postgres from the same
# internal registry mirror instead.
COMPOSE_FILE = os.environ.get("FINA_E2E_COMPOSE_FILE", "docker/compose.e2e.public.yaml")


@dataclass
class ComposeStack:
    project: str = field(default_factory=lambda: f"fina-e2e-{uuid4().hex[:12]}")
    urls: dict[str, str] = field(default_factory=dict)
    management_urls: dict[str, str] = field(default_factory=dict)
    database_url: str = ""

    def compose(self, *args: str, timeout: int = 120) -> str:
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(ROOT / COMPOSE_FILE),
            *args,
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Compose {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
            )
        return result.stdout.strip()

    def image(self) -> str:
        # Matches compose.e2e.public.yaml's x-runtime anchor (image: ${COMPOSE_PROJECT_NAME}-app)
        # directly, rather than parsing `compose config --images` -- its output order
        # isn't reliably filtered by service or service-declaration order, so picking
        # a line by position intermittently returned postgres:15-alpine instead.
        return f"{self.project}-app"

    def run_one_off(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        """Runs a throwaway container from this stack's image, detached from the
        Compose network and its env -- for exercising startup behavior (e.g.
        missing configuration) without disturbing the running replicas."""
        command = ["docker", "run", "--rm"]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        command += [self.image(), *args]
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)

    def inspect(self, service: str) -> dict[str, Any]:
        container = self.compose("ps", "--all", "--quiet", service)
        if not container or "\n" in container:
            raise RuntimeError(f"Expected one container for {service}, got {container!r}")
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout)[0]

    def start(self) -> None:
        # Build the production target once. Both replicas and the migration use this image.
        self.compose("build", "api-1", timeout=600)
        self.compose("up", "--detach", "--no-build", "api-1", "api-2")
        self._refresh_addresses()
        self.wait_ready()

    def _refresh_addresses(self) -> None:
        # Docker can assign different published ports when a container restarts.
        for service in SERVICES:
            self.urls[service] = "http://" + self.compose("port", service, "8000")
            self.management_urls[service] = "http://" + self.compose("port", service, "9000")
        address = self.compose("port", "postgres", "5432")
        self.database_url = f"postgresql://fina_e2e:fina_e2e@{address}/fina_e2e"

    def wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        pending = set(self.management_urls)
        with httpx.Client(timeout=2, trust_env=False) as client:
            while pending:
                for service in list(pending):
                    try:
                        response = client.get(self.management_urls[service] + "/probes/ready")
                        if response.status_code == 204:
                            pending.remove(service)
                    except httpx.TransportError:
                        pass
                if not pending:
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Replicas did not become ready: {sorted(pending)}")
                time.sleep(0.1)

    def stop_service(self, service: str) -> None:
        self.compose("stop", "--timeout", "5", service)

    def start_service(self, service: str) -> None:
        self.compose("start", service)
        self._refresh_addresses()
        self.wait_ready()

    def sqlalchemy_database_url(self) -> str:
        """database_url as asyncpg needs it (plain postgresql://); SQLAlchemy's
        async engine needs the +asyncpg driver suffix instead."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        connection = await asyncpg.connect(self.database_url, timeout=5, command_timeout=10)
        try:
            return [dict(row) for row in await connection.fetch(query, *args)]
        finally:
            await connection.close()

    def query(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return asyncio.run(self.fetch(query, *args))

    def reset_database(self) -> None:
        # This URL is discovered from our unique Compose project's private PostgreSQL container.
        self.query("""
            TRUNCATE call_search, orders, send_order_jobs, client_profile_jobs,
                     audio_analysis_jobs, audio_fetch_jobs,
                     calls, clients, discovery_runs, advisors
        """)

    def logs(self) -> str:
        return self.compose("logs", "--no-color", "--tail", "150", timeout=30)

    def close(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "5")
