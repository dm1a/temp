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
- FinaAPI-owned fetch and analysis models, `AudioFetcher` and `AudioAnalyzer`
  application interfaces, and analysis search-text extraction;
- unit tests, PostgreSQL repository tests, and E2E tests with two application containers.

Intentionally deferred until their contracts are finalized:

- the external AudioFetcher and AudioAnalyzer adapters;
- queue worker loops and retry-policy orchestration;
- structured order and accumulated client-profile queries.

Vault integration is wired in (`vault_secrets`, layered into `Settings` via
`SecretSettings`) but only usable once the corporate network's real
`vault-secrets` package replaces the local stand-in in `packages/vault_secrets`
-- see that package's docstring, and "Configuration" below.

## Configuration

All of fina's own settings are environment variables prefixed with `FINA_`
(for example `FINA_DATABASE_URL`, `FINA_MCP_API_KEY`) -- see
[`config.py`](src/fina/config.py) for the authoritative list. This is the one
prefix for the application itself; two other, unrelated things happen to look
similar and are documented here to avoid confusion:

- `VAULT_*` variables (`VAULT_URL`, `VAULT_ROLE_ID`, `VAULT_SECRET_ID`,
  `VAULT_ENGINE`, `VAULT_SECRET_PATH`, `VAULT_CA_BUNDLE`) are an external
  system's own naming convention, read directly by the `vault_secrets`
  package -- never `FINA_`-prefixed. `VAULT_CA_BUNDLE` is optional and
  independent of the other five: unset verifies the Vault server's TLS
  certificate against the standard CA bundle; a file path verifies against
  that CA bundle instead (the normal case, since the corporate Vault serves
  a certificate from an internal CA); the literal string `false` disables
  verification entirely, for local testing against a self-signed dev Vault
  only -- never in a real deployment.
- `FINA_TEST_DATABASE_URL` is the integration-test harness's own variable
  (see "Tests" below), read directly via `os.environ`. It is unrelated to
  `Settings` despite the shared `FINA_` text.

`Settings` (secrets: `mcp_api_key`, sourced from Vault once configured -- see
above -- otherwise a plain env var) and `DatabaseSettings` (ordinary
configuration only: `database_url`, `sql_echo`; used standalone by migration
jobs, which need no secrets) are kept as separate classes for exactly that
reason: so migrations, and anything else that only needs database
connectivity, never have to satisfy the application's secret requirements.

This settings machinery itself never reads a `.env` file -- `.env` is loaded
by the launch command instead (`uv run --env-file .env ...`, below), so a
`.env` present in the working directory can never silently leak into, say, a
test that expects a variable to be genuinely unset.

## Local setup

```bash
cp .env.example .env
uv sync
uv run --env-file .env alembic upgrade head
uv run --env-file .env uvicorn fina.main:create_app --factory --reload
```

`uv run` does not read `.env` automatically -- `--env-file .env` is required.

`uvicorn fina.main:create_app --factory` above is a single-port, `--reload`-friendly
convenience for day-to-day development: one ASGI app with every route on one port. It
is not how the container actually runs -- see "Application entry point".

## Application entry point

```bash
uv run --env-file .env python -m fina
```

This is what the container's `CMD` runs, and the only entry point that matches
"Container deployment" below: it initializes the DB engine, `RuntimeState`, and
background workers exactly once, then serves two independent ASGI servers
from that one shared container:

- **routes**, port `8000` -- the public `/api/v1/*` business API.
- **management**, port `9000` -- `/probes/*` and `/internal/tasks/*`. Keep
  this port cluster-private: its discovery trigger deliberately has no
  authentication (see "Calls discovery trigger"), and splitting it onto its
  own port makes that enforceable at the network level, not just by
  convention.

Both servers are coordinated as one unit: if either fails during startup
(for example, a port already in use), the whole process exits nonzero
without leaving the other half running; a single `SIGTERM`/`SIGINT` begins a
**bounded** shutdown of both servers and the background workers together --
see "Health endpoints" for what stops accepting new work immediately versus
what gets a grace period.

`FINA_API_MODE` picks which of two explicit modes to run in (default `true`):

- `true` (the default, and the only mode this application actually runs in
  today): routes and management are served; no background workers start.
- `false`: also starts the discovery/fetch/analysis/client-profile/
  send-order workers. This requires real `AudioFetcher`/`AudioAnalyzer` SDK
  clients, which don't exist yet -- `packages/audio_fetcher` and
  `packages/audio_analyzer` are local stand-ins for the not-yet-published
  real SDKs (see their docstrings) and cannot back real workers. Setting
  `FINA_API_MODE=false` today therefore fails immediately, before either
  port binds, with a clear error explaining why -- not a silent fallback
  to API-only behavior, and not a crash deep inside a worker loop the
  first time it touches a stand-in client.

## Health endpoints

```text
GET /probes/healthz
GET /probes/ready
```

Both live on the management port (`9000`). Successful probes return
`204 No Content`. `/probes/healthz` is deliberately shallow (no dependencies) --
use it as the liveness probe. `/probes/ready` covers what a separate startup
probe would otherwise duplicate (`runtime.started`), plus draining state and
database connectivity/schema: it fails while starting up, while shutting down
(see below), and while the database is unreachable or on the wrong Alembic
revision. The database check has its own short timeout
(`DatabaseProbe.DEFAULT_CHECK_TIMEOUT`, 2 seconds) so a slow or hung database
makes readiness fail fast rather than hang the probe itself. Additional
readiness checks for Vault secrets and worker executors will be registered
when those components are implemented.

### Bounded shutdown

On `SIGTERM`/`SIGINT` (see "Application entry point"), `runtime.draining` is
set immediately -- `/probes/ready` starts returning `503` right away, so an
orchestrator stops sending new traffic before anything else happens -- and
every worker's polling loop is told to stop claiming new jobs (a job already
claimed keeps running). Both HTTP servers drain concurrently for at most
5 seconds (`HTTP_SHUTDOWN_TIMEOUT` in `__main__.py`); outstanding requests
are cancelled after that. Workers get a bounded grace period
(`SHUTDOWN_GRACE_PERIOD` in `lifespan.py`, currently 40 seconds) to finish
in-flight jobs; a worker still running after that is cancelled outright
rather than awaited indefinitely. HTTP draining and worker cleanup together
take at most about 45 seconds, leaving room in Kubernetes' 60-second
termination budget for the 5-second preStop hook and resource cleanup.

PostgreSQL enforces a 30-second `statement_timeout`
(`DATABASE_STATEMENT_TIMEOUT` in `db/session.py`). External calls also need
a deadline: each claimed job has a five-minute processing budget
(`DEFAULT_PROCESSING_TIMEOUT` in `services/worker_loop.py`), including provider
retries and database work. Discovery shares this budget across all advisors.
This is below the shortest stale-claim window of ten minutes. Measure real
provider durations when integrating the corporate SDKs; keep any adjusted
processing budget below the stale window, with room for rollback and cleanup.
Adapters must use cancellable async I/O and propagate cancellation.

A processing timeout cancels the operation and uses the job's existing retry
limit. Discovery records a failed run, which the next discovery request can
retry. Shutdown cancellation leaves unfinished claims for `recover_stale()`
to reclaim after the stale window, just as after a crash.

## Logging and metrics

Logging is set up once at process startup by `fina/logging_config.py`'s
`configure_logging()`, which tries to import the corporate `py_logs` package
first and falls back to console JSON logging if it isn't installed -- no
setting decides this, only whether `py_logs` is actually present (installed
only in the corporate `Dockerfile` build, never in `Dockerfile.public`). As
with `vault_secrets` (see "Configuration"), the fallback exists because the
real integration isn't available outside the corporate network.

Console output (the fallback branch, `JsonFormatter`) uses one JSON record per
line with timestamp, severity, logger name, instance hostname and an explicit
allowlist of context fields; exceptions keep only their type and stack
locations, never messages or locals. `py_logs`, once installed, owns
formatting entirely instead -- it isn't asked to honor this allowlist. Job
events carry the worker ID, job ID, claim token and attempt, plus the request
version for profile jobs. Completion/retry events follow the database commit;
an ignored update from a lost or superseded claim is logged as a warning.

What gets logged, and where:

- **Process lifecycle** (`__main__.py`, `lifespan.py`): startup with the
  resolved mode and ports, which background workers started (or an explicit
  "none started" line, so an idle `api`-mode replica is never mistaken for a
  broken `processing` one), shutdown being requested, workers draining
  cleanly or blowing their grace period, and resources released.
- **HTTP requests** (`metrics.py`'s `MetricsMiddleware`): one summary line
  per request with method, matched route template, status and duration --
  at `ERROR` for 5xx, `WARNING` for 4xx and for requests cancelled by
  shutdown, `INFO` otherwise. Routine `/probes/*` and `/metrics` traffic is
  deliberately silent, so healthcheck polling can't drown out the signal.
- **Readiness transitions** (`api/probes.py`): logged when readiness
  *changes*, not on every poll, with the failure reason. A schema-revision
  mismatch additionally logs the expected and found revisions
  (`db/health.py`) -- the whole diagnosis when a migration Job hasn't run
  yet, or has run ahead of a replica's image (see `k8s/README.md`).
- **Provider calls** (`adapters/`): every call across the MTS AudioFetcher /
  FINA Analyzer boundary logs its outcome and duration -- success, a
  provider error (with error code and retryable flag), or a raised
  exception. These are network calls to a third party, and are usually the
  first thing to interrogate when a queue stalls.
- **Job processing** (`services/*_worker.py`, via `worker_logging.py`):
  claim/completion transitions, failures (`logger.exception`), stale-claim
  recovery with the recovered jobs' identifiers rather than just a count,
  and fenced updates that were skipped because the claim was lost.
- **Authentication** (`security/mcp.py`): rejected MCP requests, tagged with
  whether credentials were missing entirely or simply wrong.

Application events carry identifiers, counts, outcomes and timings. The JSON
formatter includes exception types and stack locations, without exception
messages, source lines or locals, which can contain provider payloads or SQL
parameters. HTTP summaries omit headers, query strings and raw paths;
Uvicorn's raw URL access logs are disabled. Preserve these rules when integrating
`py_logs`, and review the corporate SDKs' own logs.

`FINA_SQL_ECHO` (see "Configuration") remains a local debugging option:
SQLAlchemy's query echo can include bound parameters such as transcript/profile
text, so leave it unset in deployment.

```text
GET /metrics
```

Lives on the management port (`9000`), alongside `/probes/*`. Exposes
Prometheus text format via `prometheus_client`: `fina_http_requests_total`
(a counter) and `fina_http_request_duration_seconds` (a histogram), both
labeled by HTTP method and route. The route label is always the *matched
route template* (e.g. `/api/v1/orders`), never the raw request path, and
falls back to a single `unmatched` label for anything that didn't match a
route at all (see `fina/metrics.py`'s `MetricsMiddleware`) -- so an arbitrary
or malformed path, or a route with path parameters, can never blow up label
cardinality. Both apps (routes and management) share one process-wide
Prometheus registry, so `/metrics` reports traffic from both ports.

## Business endpoint contracts

```text
GET /api/v1/transcripts/raw
GET /api/v1/transcripts/summarized
GET /api/v1/orders
GET /api/v1/client-profile
```

All four endpoints are backed by parameterized PostgreSQL queries. They request
one extra row and return `has_next` without a count query. All business
endpoints accept limits from 1 to 100 and offsets from 0 to 100,000 (raw
transcripts default to a page size of 20; summarized transcripts and orders
default to 50; client-profile defaults to 1, since only the current snapshot
is ever returned). When both dates are provided, `date_from` must precede
`date_to`; invalid ranges return `422`. Date filtering includes `date_from`
and excludes `date_to`.

Order full-text search covers order type, instrument name, volume, price,
currency, and additional details (not `execution_date`, which has its own
date filters). Client-profile full-text search matches against the client's
current accumulated profile; no version history is stored, so the endpoint
always returns at most one item and ignores `offset` beyond 0. The orders
endpoint intentionally has no `isin` filter.

Every business request must carry the Vault-injected key:

```http
Authorization: Bearer <mcp-key>
```

The application reads it from `FINA_MCP_API_KEY` (or Vault -- see "Configuration").
Health endpoints remain public.

## Calls discovery trigger

Kubernetes CronJob calls the public-in-cluster endpoint without a body or key:

```text
POST /internal/tasks/discovery
```

The endpoint atomically enqueues a PostgreSQL discovery run and returns `204 No
Content`. Its end is the trigger time. Its start is the latest completed run's
end. Until the first completion, it uses the earliest stored run's start, including
failed runs. An empty discovery history uses `FINA_DISCOVERY_START_AT`, then the
scheduling process's startup time. The stored boundary survives restarts and
is shared by every replica; differences in replica startup times do not trigger
backfill once history exists. Configure the same `FINA_DISCOVERY_START_AT` on all
replicas to make the first-ever boundary independent of which replica schedules.
A failed identical window can be queued again.

If `FINA_DISCOVERY_START_AT` is set earlier than the earliest recorded window, the
next scheduling call backfills the gap instead of continuing forward: it creates
one run from `FINA_DISCOVERY_START_AT` to the earliest recorded window's start, and
normal forward-moving scheduling resumes automatically once that run completes.
Backfilling is safe even if it re-scans already-processed calls: call, fetch-job,
and analysis-job writes are all idempotent per call, so rediscovering an
already-completed call is a no-op and never re-triggers already-finished work.

A pending or running discovery prevents another run. Scheduling is serialized
across FinaAPI instances with a PostgreSQL transaction-scoped advisory lock under
`READ COMMITTED` isolation. A unique partial index also prevents multiple active
discovery runs through other write paths. Retain discovery history: it contains
the shared checkpoint.

Application-generated and accepted timestamps are normalized to fixed UTC+03:00.

## Audio fetch contracts

FinaAPI owns the models in [`domain/audio_fetch.py`](src/fina/domain/audio_fetch.py)
and the [`AudioFetcher`](src/fina/services/audio_fetcher.py) interface. A future
adapter will translate the external SDK's inputs, results and errors into these
models. Fetch and analysis share `CallIdentity` from
[`domain/audio_contracts.py`](src/fina/domain/audio_contracts.py). External field
names and manifest serialization belong to the adapter.

| Internal model | Contract |
| --- | --- |
| `FetchCallInput` | `source_call_id`, mapped to the external `FetchCallInput.id_call` |
| `CallIdentity` | Source call ID, aware start time, advisor phone, counterparty phone, direction |
| `FetchCallResult` | `kind="success"`, returned identity, exact `manifest_object_key` |
| `FetchCallError` | `kind="failure"`, error code/message, explicit `retryable` flag, optional HTTP status |

Identity uses the fields already stored in `calls`. Adapters must map the provider's
returned identity into those fields and the database's phone format; they must not
fill missing response fields with the expected queued identity. Start times are
normalized to UTC+03:00, and naive timestamps are rejected. Repositories return
fetch and analysis claims containing a parsed `CallIdentity`. Constructing a
`FetchCompletion(claim=claim, result=result)` compares the two identities and raises
`CallIdentityMismatchError` on a mismatch. The completion retains the matching
claim and result for the worker to save.

Source IDs and manifest keys are opaque strings and are preserved exactly. The
adapter returns success only once the audio has been uploaded and the JSON
manifest uploaded last. It maps the external result's `object_key` to
`manifest_object_key`. String validation alone cannot prove an object is a manifest;
that guarantee belongs to the external fetcher and its adapter.

The fetch worker commits its database claim before calling `fetch_call`, then
constructs a completion before completing the fetch and queuing analysis in one
transaction. With multiple replicas, an interrupted attempt can be retried by
another worker, so the adapter must tolerate repeated requests for the same source
call. PostgreSQL claim tokens remain responsible for fencing stale completions.

Contract tests are in
[`tests/test_audio_fetch_contracts.py`](tests/test_audio_fetch_contracts.py).

## Audio analysis contracts

The internal models in
[`domain/audio_analysis.py`](src/fina/domain/audio_analysis.py) define the
[`AudioAnalyzer`](src/fina/services/audio_analyzer.py) interface independently
of the external SDK draft. The adapter will map the provider's field names and
types into these models.

| Internal model | Contract |
| --- | --- |
| `AnalyzeInput` | Local call UUID as `task_id`, exact `manifest_object_key` |
| `AnalyzeResult` | `kind="success"`, schema version, echoed task ID, artifacts, aware `processed_at`, complete `provider_result` |
| `AnalyzeError` | `kind="failure"`, echoed task ID, error code/message, explicit `retryable` flag, optional HTTP status |
| `AnalysisArtifacts` | Required transcript, summary and per-call profile; optional accumulated profile and pre-order |
| `Transcript` | Full text and optional segments with start/end seconds, text and optional speaker |
| `ClientProfile` / `CustomerProfile` | Separate models holding JSON objects in `data`; external question names remain flexible |
| `PreOrder` | Optional order type, instrument name, volume, execution date, price, currency and additional details |

Transcript and summary fields are required on success, but explicitly empty text
is valid for calls without recognized speech. Missing order values remain `None`,
including currency; supplied currency codes use three uppercase letters. Segment
times must be nonnegative and their end must not precede their start. The adapter
must supply an aware processing timestamp; FinaAPI does not guess the timezone of
the draft's naive `datetime.now()` value.

The current external draft supplies a per-call `ClientProfile` but omits the
accumulated `CustomerProfile`. The internal `customer_profile` is therefore optional.
When absent, profile search text is empty; per-call answers are not substituted for
an accumulated profile. The aggregation contract remains deferred.

The current external draft's `AnalysisArtifacts.transcript` is a plain string with no
segment boundaries or speaker labels, so the adapter maps it to `Transcript.text` with
an empty `segments` tuple; the internal model still supports segments for when the
draft (re-)gains structured transcript output.

Constructing `AnalysisCompletion(claim=claim, result=result)` checks the returned
task ID against the claim. The external draft's `analyze()` no longer echoes a call
identity, so this is a task ID check only -- unlike `FetchCompletion`, which still
cross-checks the fetched identity against the queued one (see `CallIdentity` in
[`domain/audio_contracts.py`](src/fina/domain/audio_contracts.py)). The analysis
claim names the stored manifest reference `manifest_object_key`, matching the
adapter input. Error handling uses `error.validate_task(...)` before changing retry
or failure state. The adapter must map these identifiers from the response and
preserve the exact manifest key when passing it to the external
`AnalyzeInput.object_key`. Provider status codes map to `error_code`; `http_status`
is reserved for actual HTTP status codes.

`result.to_storage()` produces a JSON-compatible envelope for the existing
`audio_analysis_jobs.analysis_result` JSONB column. It includes the normalized result
with `schema_version="1"` and the full provider JSON under `provider_result`, including
unknown fields. The adapter captures that payload before extracting normalized
fields. FinaAPI UUIDs and dates are serialized for JSON; the provider JSON values
are preserved. Non-JSON values and non-finite numbers must be resolved by the
adapter rather than silently converted or lost. `analysis_schema_version` records
this internal envelope version, independently of any version in the provider payload.

[`build_search_text`](src/fina/services/analysis_indexing.py) extracts transcript
and summary text, all non-null scalar values of `CustomerProfile` (recursively,
without field names), and only `PreOrder.instrument_name` for orders. Object keys
are traversed in sorted order for deterministic output; array order is preserved.
The returned fields include `search_schema_version="1"` and can be passed to
`complete_and_index` alongside the storage envelope in the worker's transaction.

These are contracts and pure data transformations; worker execution and SDK adapters
are separate steps. Credentials and SDK initialization belong to the adapters.
CRM delivery remains deferred. Tests cover the contracts, search extraction and
storage through real PostgreSQL JSONB in
[`test_audio_analysis_contracts.py`](tests/test_audio_analysis_contracts.py),
[`test_analysis_indexing.py`](tests/test_analysis_indexing.py) and
[`test_analysis_contract_storage.py`](tests/integration/test_analysis_contract_storage.py).

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

Each discovery, fetch, or analysis claim returns a unique `claim_token`. Completion,
retry, and failure methods require that token and the worker ID. Recovery clears
the token, so a late result cannot update a job reclaimed by any replica, even if
worker IDs are reused. Worker implementations must commit a claim before external
work, then finalize it in a new transaction using its token. A `False` result means
the claim no longer owns the job. Fetch completion and analysis enqueue, and
analysis completion and search indexing, each execute atomically.

Fetch and analysis repositories parse stored call identities when claiming work.
A malformed identity marks that job `FAILED` with `INVALID_CALL_IDENTITY` in the
claim transaction; the next poll can claim another job. Provider failures marked
retryable are requeued immediately while the job has attempts remaining. Providers
handle their own retries before returning a failure; workers add no backoff delay.

## Container deployment

Deployment assumes **at least two application replicas** behind a load balancer,
sharing one PostgreSQL database and the same injected MCP key. Coordination must
remain in PostgreSQL; process memory and container files must not hold checkpoints
or locks shared across replicas. Queue worker loops remain deferred as listed above.

```bash
docker build -t fina .
```

The image runs as a non-root user (`python -m fina`, see "Application entry point")
serving business routes on port `8000` and management routes on port `9000`. Scale
the container replicas through your orchestrator. Configure `/probes/ready` (readiness)
and `/probes/healthz` (liveness) -- both on port `9000` -- as the corresponding probes,
and give the pod a `terminationGracePeriodSeconds` comfortably above the shutdown grace
period described in "Bounded shutdown" (40 seconds), or Kubernetes will `SIGKILL` before
in-flight jobs get their chance to finish or be cleanly abandoned. Expose only port
`8000` publicly; keep port `9000` cluster-private -- its discovery trigger deliberately
does not require the MCP key.

Run `alembic upgrade head` **once as a separate deployment job** using the same
image and `FINA_DATABASE_URL`, before starting the replicas. Migration
jobs do not require `FINA_MCP_API_KEY` or any Vault configuration -- see
"Configuration" -- since the migration job only ever constructs
`DatabaseSettings`, not `Settings`. Do not run migrations in every replica's
startup command. The single initial migration, `0001_initial_schema`, creates the
complete schema, including claim tokens, claim-state constraints, the
single-active-discovery constraint, and the transcript lookup index.

Minimal Kubernetes manifests (Deployment, Services, ConfigMap/Secret
references, migration Job) live under [`k8s/`](k8s/), along with the
maintenance procedure a future schema-changing release needs: `/probes/ready`
requires an *exact* Alembic revision match, so a schema change and a rolling
update can never both be zero-downtime at once with this readiness check --
see `k8s/README.md` rather than building schema-compatibility logic into the
app for this MVP.

## Tests

Repository tests are in [`tests/integration/repositories/`](tests/integration/repositories/):

| Test file | Repository coverage |
| --- | --- |
| [`test_discovery_runs.py`](tests/integration/repositories/test_discovery_runs.py) | Discovery windows, concurrent scheduling, claim ownership, two app instances |
| [`test_jobs.py`](tests/integration/repositories/test_jobs.py) | Fetch and analysis claims, retries, stale recovery, atomic completion, rollback |
| [`test_records.py`](tests/integration/repositories/test_records.py) | Advisors, clients, calls, duplicate writes, client origin, transaction rollback |
| [`test_transcripts.py`](tests/integration/repositories/test_transcripts.py) | Phone/date filters, full-text search, ordering, pagination, database-backed endpoints |

These tests execute repository methods against real PostgreSQL connections. The
initial schema's upgrade/downgrade test is in
[`tests/integration/test_migrations.py`](tests/integration/test_migrations.py).

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Without `FINA_TEST_DATABASE_URL`, PostgreSQL integration tests are skipped. To run
the repository and migration tests against a test server, supply a `postgresql+asyncpg` URL whose
role can create databases. Each test creates, migrates, and removes its own database;
the supplied database is used only to administer those isolated databases.

To run only repository tests with that environment variable configured:

```bash
uv run pytest tests/integration/repositories -rs
```

The container test runner provides PostgreSQL and runs lint, formatting, unit tests,
and integration tests, including two independent app instances and separate worker connections:

```bash
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down --volumes
```

On a machine with no path to the public internet (e.g. inside the corporate
network), use [`compose.test.corporate.yaml`](compose.test.corporate.yaml)
instead -- it builds [`Dockerfile`](Dockerfile)'s `test` stage and pulls
postgres from the same internal registry mirror `Dockerfile` uses for python,
rather than `Dockerfile.public` and public Docker Hub:

```bash
docker compose -f compose.test.corporate.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.corporate.yaml down --volumes
```

### End-to-end tests

[`tests/e2e/test_application.py`](tests/e2e/test_application.py) exercises the built
production image through real HTTP connections to two separate application
containers. Run it from a host or CI runner with Python dependencies installed
and a running Docker daemon with Docker Compose v2:

```bash
uv sync --frozen
uv run pytest tests/e2e --run-e2e --strict-markers
```

On a machine with no path to the public internet, set `FINA_E2E_COMPOSE_FILE`
so the suite builds [`Dockerfile`](Dockerfile) via
[`compose.e2e.corporate.yaml`](compose.e2e.corporate.yaml) instead of
`Dockerfile.public` via `compose.e2e.yaml`:

```bash
FINA_E2E_COMPOSE_FILE=compose.e2e.corporate.yaml uv run pytest tests/e2e --run-e2e --strict-markers
```

The six E2E scenarios cover:

- a fresh database, one successful migration job without the MCP key, and two
  ready replicas running the same non-root production image;
- authenticated raw and summarized transcript requests against both replicas,
  including phone/date filters, Russian full-text search, and pagination;
- concurrent discovery HTTP requests creating exactly one shared run;
- an instance restart preserving a failed discovery's original boundary, and
  the surviving instance continuing from the latest completed boundary;
- a PostgreSQL outage returning readiness `503` while liveness stays `204`,
  followed by recovery without reapplying migrations.

The suite owns a unique Compose project defined in
[`compose.e2e.yaml`](compose.e2e.yaml) (or `compose.e2e.corporate.yaml`, above),
with a fresh PostgreSQL volume and randomly
assigned localhost ports. It builds the runtime image once, runs migrations once,
resets only its own test data between scenarios, captures container logs on failure,
and removes its containers, network, and volume afterward. It does not require
`FINA_TEST_DATABASE_URL`. An explicitly enabled E2E run fails if Docker or startup
is unavailable; ordinary `uv run pytest` skips E2E tests unless `--run-e2e` is supplied.

Transcript fixtures represent completed worker output, and discovery worker outcomes
are set in the test database. The full audio processing workflow will need an E2E
scenario when the worker loops are implemented.
