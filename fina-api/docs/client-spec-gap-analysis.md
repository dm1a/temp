# Client specification comparison

Reviewed: 2026-09-09. Scope: client requirements 5.1–6.5, compared with the current
`src/fina` code, migration, adapters, tests, and container configuration.

“Implemented” below means that the application code exists. It does not establish
that external services or production deployment satisfy the requirement. The two
SDK modules in `src/fina/adapters` contain contract models and Python protocols;
they do not implement telephony, object storage, model inference, or CRM requests.

## Overall assessment

The application has all four business endpoints, persistent queues for discovery,
fetching, analysis, profile aggregation and CRM delivery, adapter mapping code,
PostgreSQL JSONB storage, and indexed transcript/order search. It remains incomplete
against the client specification. The largest gaps are real service wiring,
complete raw transcript responses, reliable profile updates, CRM validation and
retry behavior, access control and audit, operational deployment, and measured ML
quality/performance.

The default Docker command calls `create_app()` without `audio_fetcher` or
`audio_analyzer`. Both default to `None`, so the default deployment starts the HTTP
API without starting the processing workers. Adapter implementations need real SDK
clients, configuration and application-factory registration. See
[main.py](../src/fina/main.py), [Dockerfile.public](../.docker/Dockerfile.public),
[MTS SDK contract](../src/fina/adapters/mts_audio_fetcher_sdk.py) and
[Analyzer SDK contract](../src/fina/adapters/fina_analyzer_sdk.py).

## Functional requirements

| Requirement | Current implementation | Gap or remaining evidence |
| --- | --- | --- |
| 5.1 Telephony REST integration | Discovery/fetch workers and MTS adapter mapping exist. | Real REST client and deployment wiring are absent from this repository. |
| 5.1 WAV and MP3 | Audio fetching/storage is delegated to the SDK. | Neither format is exercised against a real fetcher here; format support and encrypted object storage need integration evidence. |
| 5.1 Push **or** scheduled pull | A discovery trigger and pull worker exist. | A production scheduler/CronJob is documented but not supplied. A push endpoint is absent, but both modes are not required if scheduled pull is chosen. |
| 5.1 Receipt acknowledgment and recognition initiation | The trigger acknowledges scheduling with HTTP 204. Successful fetch atomically queues analysis. | Scheduling acknowledgment is not an audio-receipt acknowledgment. The actual telephony receipt/acknowledgment behavior must be implemented or documented with the SDK. |
| 5.1 Metadata and phone identity | Calls store source ID, start time, both phones, direction, filename and call/recording durations. Adapter mapping normalizes leading `+`; fetch/analysis workers check returned identity. | Provisioning the active-advisor list and external identity matching still need deployment/integration setup. |
| 5.2 Local GigaAM transcription and voice-based diarization | Contracts support transcript text, segments, times and speaker roles. | No GigaAM/diarization implementation or deployment is included. Segment labels alone do not prove voice-based advisor/client identification. |
| 5.2 Russian recognition quality and processing within 3 minutes | The processing architecture is asynchronous. | No representative Russian audio evaluation, WER report, processing-time benchmark, or readiness notification exists here. |
| 5.3 Persistent transcripts and metadata | Successful analysis stores a versioned JSONB envelope containing normalized artifacts and the provider result. | Persistence of actual GigaAM output remains to be exercised with the real SDK. Partial-stage persistence also needs an agreed contract. |
| 5.3 Phone/date/full-text search and indexes | Phone/date filters, Russian full-text search, B-tree indexes and generated TSVECTOR/GIN indexes exist. | The 2-second search target is unmeasured at expected production data volumes. Native PostgreSQL full-text search meets the database technology requirement; adding `pg_trgm` is not inherently required. |
| 5.4 Automatic profile enrichment | Successful analysis queues profile aggregation; the profile is stored against the client phone/ID. | New-call notifications can be lost while aggregation is active; stale calculations can overwrite newer profiles. See correctness findings below. |
| 5.4 Incremental accumulated profile | The worker recomputes a snapshot using the latest 10 completed call profiles. | It does not merge an existing accumulated profile with a new call. This fits a rolling-window interpretation of 5.6, but may discard older facts expected by 5.4. Agree the retention policy and N. |
| 5.4 Local summarization model | The Analyzer adapter exposes analysis and profile aggregation. | Model choice, local inference, required profile content and no external data transfer are not established by this repository. |
| 5.5 Structured order extraction | Internal and SDK models cover type, instrument, volume, execution date, price, currency and details; orders are persisted. | Extraction quality is external. The SDK contract accepts only RUB, while the internal model permits other currency codes; the business scope needs agreement. |
| 5.5 Create CRM requests only with mandatory fields | Any non-null pre-order currently queues CRM delivery. | No mandatory-field completeness gate exists. An incomplete `PreOrder` can be sent. No explicit log records rejection due to incomplete order information. |
| 5.5 CRM payload includes advisor and client identifiers | The outgoing contract includes client phone and an order ID derived from the call UUID. | There is no explicit advisor identifier in the outgoing contract. Real HTTP delivery and CRM duplicate suppression are unverified. |
| 5.5 CRM timeouts and exponential retry | Persistent delivery jobs and a maximum attempt count exist. | Retries become immediately eligible; no exponential backoff or external-call deadline is implemented in Fina. Comments assume SDK retries, but no SDK implementation proves the required behavior. |

Sources: [discovery worker](../src/fina/services/discovery_worker.py),
[fetch worker](../src/fina/services/fetch_worker.py),
[analysis worker](../src/fina/services/analysis_worker.py),
[profile worker](../src/fina/services/client_profile_worker.py),
[CRM worker](../src/fina/services/send_order_worker.py),
[Analyzer adapter](../src/fina/adapters/audio_analyzer_adapter.py), and
[initial schema](../alembic/versions/0001_initial_schema.py).

## REST API comparison: 5.6

| Endpoint | Matches | Missing or different |
| --- | --- | --- |
| `/api/v1/transcripts/raw` | Required advisor/client phones; date range; search; limit **20**; offset 0; authentication; JSON pagination. | Returns one text string, without speaker segments or their timestamps. Word-level search positioning is also absent. Stored segment data is not exposed by this response. |
| `/api/v1/transcripts/summarized` | Required phones; date range; summary search; limit **50**; offset 0; authentication; JSON pagination. | No direct query-contract mismatch found. Real summarization output and performance remain unverified. |
| `/api/v1/orders` | Optional phones/date range; BUY/SELL filter; limit **50**; offset 0; persisted structured fields; indexed search across several order fields. | Search extraction omits `execution_date`, so it does not literally search every order field. ISIN is absent, but the client explicitly places it in MVP2. |
| `/api/v1/client-profile` | Required client phone; current profile; search; limit **1**; offset 0; authentication. | No `date` parameter or historical/as-of retrieval. Only one snapshot is stored. Search uses the current JSON object's text, including keys, and cannot search update history. For a current-only response the client note says pagination may be ignored; current code returns an empty page when offset is nonzero. |

The specification permits omitting profile history, but also requests `date` and
history search. That choice needs agreement: a current-only snapshot cannot answer
historical queries. Raw transcript search is already implemented even though the
client labels it MVP2. There is no MCP tool exposure configuration in this
repository, so exclusion of the raw endpoint from MCP must be checked in the MCP
application that exposes these APIs.

Sources: [query models](../src/fina/api/query_models.py),
[response models](../src/fina/api/response_models.py),
[transcript endpoints](../src/fina/api/v1/transcripts.py),
[profile endpoint](../src/fina/api/v1/client_profile.py),
[search extraction](../src/fina/services/analysis_indexing.py), and
[client repository](../src/fina/repositories/clients.py).

## Reliability findings for two or more instances

The queue repositories use `FOR UPDATE SKIP LOCKED` and claim tokens. These are
useful foundations, but the workers do not consistently protect all resulting
writes with those tokens.

1. **Stale analysis can still create downstream work.**
   `AnalysisWorker._complete()` ignores the Boolean result of
   `complete_and_index()`. Even when that method rejects a stale claim, the worker
   proceeds to enqueue profile work, upsert a order and enqueue CRM delivery. All
   downstream effects must be conditional on accepting the claim.

2. **A late profile result can overwrite a newer result.**
   `ClientProfileWorker.run_once()` writes the client profile without validating
   ownership of the claim, then completes the job in another transaction. Claim
   tokens on job completion do not protect that earlier profile write. Profile
   publication and job completion need an atomic ownership/version check.

3. **A new call can fail to trigger a profile refresh.**
   `ClientProfileJobRepository.enqueue()` does nothing when the client's job is
   already in progress. If that job has already read its input calls, an analysis
   completed during aggregation is omitted, and no follow-up run is queued.
   Track a requested history version or pending refresh so the newer call is
   eventually included. Reset attempt counters when beginning a new aggregation
   generation, rather than sharing one lifetime retry count for the client.

4. **Slow calls can be reclaimed while still running.**
   Workers use fixed stale thresholds without heartbeats or bounded external-call
   execution. Recovery compares application time with database lock timestamps.
   Establish a timeout/lease policy using consistent time, and ensure late work
   cannot publish results after recovery. Stale recovery currently also permits
   repeated claims beyond the normal failure retry budget.

5. **Shutdown and health do not detect a stalled worker adequately.**
   Shutdown waits for all worker tasks without a deadline. Readiness checks
   runtime flags and PostgreSQL, not worker progress or termination. A hanging SDK
   call can delay shutdown indefinitely, and a nonfunctioning processing path can
   coexist with a healthy HTTP readiness response.

Sources: [analysis completion](../src/fina/services/analysis_worker.py),
[profile publication](../src/fina/services/client_profile_worker.py),
[profile enqueue](../src/fina/repositories/client_profile_jobs.py),
[application lifecycle](../src/fina/main.py), and
[health checks](../src/fina/api/probes.py).

## Nonfunctional requirements

| Requirement | Assessment |
| --- | --- |
| 6.1 Asynchronous processing and horizontal scaling | Implemented structurally through separate worker tasks and shared database queues. The ownership/publication defects above need fixing before claiming reliable operation across replicas. |
| 6.1 100 calls/hour and 2-second API searches | No load-test evidence. There is one sequential analysis worker per application instance. If analysis consumes the allowed 3 minutes, two such workers handle at most 40 calls/hour; 100/hour would require at least five concurrent analyses before headroom and queuing effects. This is a capacity calculation, not a measured benchmark. |
| 6.2 Durable state, retries, error logs | Present in application code. Exponential delay, call deadlines, retry classification and recovery budgets need additional work. |
| 6.2 Daily backups and RTO <=1 hour | No backup schedule, recovery automation or measured restore drill is provided. These may belong to infrastructure, but are required delivery evidence. |
| 6.3 Authentication | A shared Bearer API key protects all four business routes, satisfying the API-key authentication option. |
| 6.3 Role-based access and audit | Missing. The shared key carries no admin/advisor/manager identity or permissions. There is no access audit trail. Filtering by an arbitrary supplied phone number is not authorization. |
| 6.3 TLS 1.2+, encrypted audio, local-only processing | No production TLS/ingress configuration, object-store encryption configuration or deployment evidence establishing the local boundary is supplied. These can be fulfilled by infrastructure and SDKs, but are not verified by the current code. |
| 6.4 Structured stage logs | Error/recovery logging exists and sometimes supplies contextual `extra` fields. There is no configured structured log output covering every requested start/finish stage or data-access audit. |
| 6.4 Prometheus/Grafana metrics and alerts | No metrics endpoint/instrumentation, dashboards, CPU/GPU collection configuration or alert rules are supplied. Health endpoints do not replace these. |
| 6.5 Open-source local ML stack | The API/adapters use Python. Actual GigaAM, diarization/VAD, emotion model, word-level alignment, Triton/ONNX and local model serving are external and unverified here. The AI team's stated choice among deployment options needs recording rather than treating every named option as mandatory. |

## Decisions needed from the client/team

- **Recognition quality:** “WER no worse than 85%” mixes recognition accuracy with
  an error-rate metric. Agree the exact metric, threshold and representative
  Russian test dataset before acceptance.
- **Load:** clarify whether “100 simultaneous calls per hour” means 100 arrivals
  per hour, 100 concurrently active calls, or a burst requirement.
- **Profiles:** agree N, rolling-window versus lifetime memory, the meaning of
  “latest”/“average”, and whether historical/as-of retrieval is in the MVP.
- **CRM:** agree the required fields, identifier mapping, allowed currencies and
  idempotency contract. CRM implementation was previously deferred in this
  conversation, but it is explicitly required by the supplied client document.
  This comparison records the gap without activating delivery.
- **ML completion:** agree how silence, partial analysis, and the MVP readiness
  notification are represented.
- **Ownership:** assign production telephony scheduling, local ML serving,
  encryption/TLS, backups, monitoring and MCP exposure to concrete deliverables.

## Suggested implementation order

1. Fix claim ownership and reliable profile refresh so parallel instances cannot
   publish stale data or lose updates.
2. Connect the real SDK clients and configure startup, scheduling, deadlines and
   retries; exercise real WAV and MP3 recordings through the full pipeline.
3. Expose raw transcript speakers/timestamps and settle profile date/history
   behavior; complete the agreed order-search scope.
4. Finish the CRM completeness gate, advisor/client identifiers and delivery
   policy when that integration step is authorized.
5. Complete role enforcement, access audit and operational deployment controls
   before production; run representative ML, load and restore acceptance tests.

## Verification performed

- Inspected current code and generated OpenAPI parameters/response fields.
- Ran the existing non-integration, non-E2E suite: **88 passed, 86 deselected**.
- Database integration, container E2E, real SDK/ML, load and disaster-recovery tests
  were not run in this review. Existing tests do not establish those client
  acceptance targets; several worker tests explicitly expect immediate retries.
- No application code was changed for this comparison.
