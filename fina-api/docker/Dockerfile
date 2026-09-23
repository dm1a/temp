FROM cr.ci.sberinsur.local/dockerhub/library/python:3.12.13-slim-bookworm AS build

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --trusted-host nexus.dev.sberinsur.local \
        -i https://developer:developer@nexus.dev.sberinsur.local/repository/pip3-proxy/simple \
        --upgrade pip "uv==0.11.29"

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock README.md ruff.toml ./
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

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

RUN chmod -R a-w /app/alembic.ini /app/alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost

EXPOSE 8000 9000

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/probes/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "fina"]
