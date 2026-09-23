# Corporate production build: uses the internal registry mirror and Nexus
# PyPI proxy instead of public sources, for deployment inside the corporate
# network. For local dev and this repo's own e2e suite (compose.e2e.yaml),
# use Dockerfile.public instead -- it pulls from public registries this
# file's hosts won't resolve outside that network, and doesn't need any of
# the corporate-only pieces below. On a machine with no path to the public
# internet, run tests against this file's `test` stage instead (see
# compose.test.corporate.yaml).
#
# Preserves everything Dockerfile.public guarantees: non-root user (uid
# 10001, matching k8s/deployment.yaml's securityContext), a real venv
# (nothing editable/path-hacked), a locked (--frozen) dependency install,
# and the Alembic files migrations need. See README's "Container
# deployment" and k8s/README.md for how this image is expected to run.

FROM cr.ci.sberinsur.local/dockerhub/library/python:3.12.13-slim-bookworm AS build

# uv pinned exactly, matching the root Dockerfile's ghcr.io/astral-sh/uv:0.11.29 --
# an unpinned `pip install uv` would make the "locked (--frozen) dependency
# install" guarantee depend on whatever uv version happens to be latest on
# the day of the build.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --trusted-host nexus.dev.sberinsur.local \
        -i https://developer:developer@nexus.dev.sberinsur.local/repository/pip3-proxy/simple \
        --upgrade pip "uv==0.11.29"

# No UV_CACHE_DIR override here: uv's default cache lives at /root/.cache/uv
# (running as root, this stage), which is exactly what the --mount=type=cache
# below targets. Setting UV_CACHE_DIR anywhere before the two `uv sync` runs
# would silently point uv's cache outside that mount, making it a no-op.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Lock files and the workspace members (audio_fetcher/audio_analyzer/
# vault_secrets -- local stand-ins for not-yet-published corporate SDKs,
# see their own docstrings) first: --no-install-project resolves and
# installs every dependency, including those, without installing the root
# "fina" project itself. This layer then stays cached across ordinary
# source changes -- only pyproject.toml/uv.lock/packages changing busts it.
COPY pyproject.toml uv.lock README.md ruff.toml ./
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Only now does a source change invalidate a layer, and only this cheap one:
# every third-party (and workspace-member) dependency is already installed.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Test runner, mirroring Dockerfile.public's `test` stage: same sync (the
# `dev` dependency group is included by default; only --no-dev above opts
# out of it) and pytest entrypoint, but built off this file's corporate
# `build` stage so it works with no path to the public internet (see
# compose.test.corporate.yaml). uv itself is already installed via the
# Nexus proxy above, so the sync below goes through the same resolution
# path the --no-dev sync already relies on.
FROM build AS test
RUN uv sync --frozen --no-editable
COPY alembic.ini pytest.ini ./
COPY alembic ./alembic
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest", "--strict-markers"]

FROM cr.ci.sberinsur.local/dockerhub/library/python:3.12.13-slim-bookworm AS runtime

RUN addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --gid 10001 --no-create-home app

WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic

# Read-only migration files -- nothing in this image should ever need to
# write to them, only to the database. (Not chmod'ing the whole .venv too:
# it's thousands of files and this ran ~18s slower for it in testing, for
# protection k8s/deployment.yaml's readOnlyRootFilesystem already gives at
# the orchestrator level. Only a bare `docker run` with no such flag would
# benefit, and it isn't worth that cost on every build for that case.)
RUN chmod -R a-w /app/alembic.ini /app/alembic

# NO_PROXY: a corporate egress proxy (HTTP_PROXY/HTTPS_PROXY) commonly gets
# set on containers in this kind of network. Without this, the HEALTHCHECK
# below (and any stray localhost call) would get routed through that proxy
# instead of hitting the process directly, and typically fail or hang since
# a proxy can't reach a loopback address inside this container.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost

EXPOSE 8000 9000

USER 10001:10001

# Liveness only -- deliberately not /probes/ready, which depends on the
# database; a HEALTHCHECK restarting the container wouldn't fix a database
# problem, and Kubernetes deployments should rely on the manifests'
# startupProbe/readinessProbe/livenessProbe (k8s/deployment.yaml) instead of
# this instruction, which only matters for a plain `docker run` deployment.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/probes/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "fina"]
