# FinaAPI

Python implementation of the currently agreed FinaAPI foundation.

Implemented in this revision:

- async FastAPI application factory;
- Dishka application and request scopes;
- application-scoped SQLAlchemy async engine;
- request-scoped SQLAlchemy `AsyncConnection`;
- status-only liveness, readiness, and startup endpoints;
- raw-SQL Alembic migration for the agreed PostgreSQL schema;
- raw-SQL repositories for discovery, clients, calls, fetch jobs, analysis jobs,
  advisor lookup, transcripts, and summaries;
- validated query contracts for all specified business endpoints;
- Bearer-key authentication for all `/api/v1/*` endpoints;
- unauthenticated Kubernetes trigger for scheduling call discovery;
- implemented raw and summarized transcript endpoints with `has_next` pagination;
- unit tests that do not require a running PostgreSQL instance.

Intentionally deferred until their contracts are finalized:

- AudioFetcher and AudioAnalyzer interfaces;
- queue worker loops and retry-policy orchestration;
- direct Vault integration; deployment injects the MCP key as an environment variable;
- API response models and wiring repositories to the business endpoints;
- structured deal and accumulated client-profile queries.

## Local setup

```bash
cp .env.example .env
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn fina_api.main:create_app --factory --reload
```

## Health endpoints

```text
GET /internal/health/live
GET /internal/health/ready
GET /internal/health/startup
```

Successful probes return `204 No Content`. Liveness is deliberately shallow.
Readiness verifies application state, PostgreSQL connectivity, and that the
database is at the expected Alembic revision. Additional readiness checks for
Vault secrets and worker executors will be registered when those components are
implemented.

## Business endpoint contracts

```text
GET /api/v1/transcripts/raw
GET /api/v1/transcripts/summarized
GET /api/v1/deals
GET /api/v1/client-profile
```

Raw and summarized transcript endpoints are backed by parameterized PostgreSQL
queries. They request one extra row and return `has_next` without a count query.
Deal and client-profile endpoints still return `501 Not Implemented` until their
corrected Analyzer result contracts are agreed.

Deal full-text search is defined only over AudioAnalyzer's `instrument_name`.
Client-profile full-text search is defined over all values of `CustomerProfile`.
The deals endpoint intentionally has no `isin` filter.

Every business request must carry the Vault-injected key:

```http
Authorization: Bearer <mcp-key>
```

The application reads it from `FINA_MCP_API_KEY`. Health endpoints remain public.

## Calls discovery trigger

Kubernetes CronJob calls the public-in-cluster endpoint without a body or key:

```text
POST /internal/tasks/discovery
```

The endpoint atomically enqueues a PostgreSQL discovery run and returns `204 No
Content`. Its end is the trigger time. Its start is the latest completed run's
end, then `FINA_DISCOVERY_START_AT`, then this process's startup time. A pending
or running discovery prevents another overlapping run. Scheduling is serialized
across FinaAPI instances with a PostgreSQL transaction-scoped advisory lock.

Application-generated and accepted timestamps are normalized to fixed UTC+03:00.

## Raw SQL and transactions

Alembic and runtime repositories use PostgreSQL-specific raw SQL. Repository
methods never commit independently. A service coordinating multiple repository
operations must own the transaction:

```python
async with connection.begin():
    client_id = await clients.get_or_create(phone_normalized=client_phone)
    call_id = await calls.add_discovered_call(...)
    await clients.set_origin_if_missing(client_id=client_id, call_id=call_id)
    await fetch_jobs.create(call_id=call_id)
```

## Tests

```bash
uv run pytest
uv run ruff check .
```
