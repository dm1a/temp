# Database migrations through Helm

Development deployment runs `alembic upgrade head` through
[the migration hook](fina-migration.yaml). The existing
`deploy:develop` job is overridden locally in [`.gitlab-ci.yml`](../.gitlab-ci.yml).
It retains the shared job's manual feature-branch rules, `deploy-dev` stage,
`build_image` prerequisite, runner image, registry authentication and environment.
There is no separate migration CI job or shell helper.

## Development pipeline

The deployment job:

1. Selects the development Kubernetes context and computes the application version
   using the same convention as the shared build job.
2. Pulls the common chart, updates its metadata and copies the migration template
   into its `templates/` directory.
3. Calls `helm upgrade --install` with the existing development values, the
   application image and a six-minute timeout.
4. Helm runs the `pre-install,pre-upgrade` migration Job to completion before
   creating/updating application resources. A failed hook fails the Helm command;
   the job prints migration diagnostics and stops.
5. After successful Helm completion, `kubedog` tracks application readiness.

The application image repository/tag and `finaMigrationImage` are set together.
The job uses `resource_group: fina-develop` to serialize development releases,
`interruptible: false` to avoid automatic cancellation mid-migration, and a
15-minute job timeout to cover migration plus rollout tracking.

The override applies to **development only**. Preproduction and production jobs
continue to come from the shared template. Their environment values must be
completed before adapting the hook to those jobs.

## Configuration on first install and upgrade

The application's `config-fina` ConfigMap is part of the same Helm release.
A pre-install hook runs before it exists, and a pre-upgrade hook runs before it
is updated. To avoid missing or stale settings, the migration Job renders these
non-secret values directly from `config-gen.extraConfigMaps.config-fina`:

- `FINA_DB_HOST`, `FINA_DB_PORT`, `FINA_DB_USER`, `FINA_DB_NAME`;
- `VAULT_URL`, `VAULT_ENGINE`, `VAULT_SECRET_PATH`.

Missing required settings or `finaMigrationImage` fail chart rendering. The Job
reads AppRole credentials from the existing `common-vault` Secret, keys
`VAULT_ROLE_ID_AI_AGENTS` and `VAULT_SECRET_ID_AI_AGENTS`, and uses the existing
`harbor` image pull Secret. Provision those Secrets in the target namespace
before deployment. The configured Vault secret must contain `db_password`.

`DatabaseSettings` resolves the password in the same way as the application,
without requiring its MCP API key. The database role must have permission to
apply migrations. Keep `alembic/` and `alembic.ini` in the application image.
A `database_url` supplied by Vault overrides the individual connection fields;
check for a stale override if the application connects to an unexpected database.

## Failure diagnosis and retry

The hook Job is named `<release>-migrate`, for example `fina-adapter-migrate`.
It runs as the existing non-root user with a read-only filesystem, allows two
retries and has a five-minute execution deadline. Helm's six-minute timeout
allows margin for Kubernetes to report completion.

Successful hook Jobs are deleted. Failed Jobs are retained for up to one hour;
the next hook invocation deletes the previous Job before creating another.
CI prints their description and logs when Helm fails. For further diagnosis:

```bash
kubectl --context "$K8S_CLUSTER_DEV" -n ai-agents describe job/fina-adapter-migrate
kubectl --context "$K8S_CLUSTER_DEV" -n ai-agents logs job/fina-adapter-migrate --all-containers=true
```

After an interrupted deployment, check the previous Job and Helm release state
before retrying. A CI cancellation does not itself cancel a Kubernetes Job.
When the schema is already current, `alembic upgrade head` has no changes to
apply. A Helm rollback does not undo database migrations.

## Local verification

Install development dependencies with `uv sync`. The deployment tests execute
the real CI script with fake cluster commands and check that Helm failures stop
rollout tracking. With Helm installed, the same test file also renders the real
hook for install and upgrade, verifies its image and current configuration, and
checks that incomplete values fail rendering:

```bash
uv run pytest tests/test_migration_pipeline.py
```

If Helm is not on `PATH`, set `FINA_TEST_HELM` to its executable path. Only the
rendering tests are skipped when Helm is unavailable. These checks do not access
a cluster or the private registry.

## Diagnose readiness failures

A successful `helm upgrade` followed by `/probes/ready` returning `503` means
the rollout has not become ready. After startup, this endpoint connects to
PostgreSQL and runs `SELECT version_num FROM alembic_version`; the value must
be `0001_initial_schema` for this image. API mode still requires this check.

Read the readiness transition in the application logs. The failure type and,
for wrapped database errors, SQLSTATE are included in the message itself, so
they remain visible when the corporate console formatter omits `extra` fields.
Driver messages, SQL parameters and credentials are not logged.

| Log reason | Check |
| --- | --- |
| `ProgrammingError (SQLSTATE 42P01)` | The query cannot find `alembic_version`. Verify the database and schema/search path, then run the migration Job above if the schema has not been initialized. |
| `DatabaseSchemaMismatch` | Read the accompanying `expected=...` and `found=...` message. Match the migration and application image; `found=None` means the version table is empty. |
| SQLSTATE `42501` | The application database role lacks required permissions. |
| `TimeoutError`, `ConnectionRefusedError`, `gaierror` | Check database connectivity, hostname, port and response time from the pod. The database probe has a two-second deadline. |
| `InvalidPasswordError` | Check the database role and the `db_password` secret supplied by Vault. |

SQLSTATE meanings are defined in the
[PostgreSQL error code reference](https://www.postgresql.org/docs/current/errcodes-appendix.html).

The development ConfigMap uses `FINA_DB_HOST`, `FINA_DB_PORT`, `FINA_DB_USER`
and `FINA_DB_NAME`, with `db_password` supplied by Vault. `FINA_DB_URL` and
`FINA_DB_DB` are not settings recognized by this application. The chart's
`sharedEnvVars` must reference the ConfigMap keys to pass them to the pod.
A configured `database_url` (including `FINA_DATABASE_URL` or a value supplied
by Vault) takes precedence over these separate connection fields; check for a
stale override when the pod still connects to the wrong database.

The local `deploy:develop` override inserts the migration hook into the chart.
Check the Helm hook failure and its Job logs before retrying a failed release.
A successful migration must use the same database and image as the application.

## Releases that change the schema

The application's `/probes/ready` endpoint requires the database's
`alembic_version` to exactly match `CURRENT_SCHEMA_REVISION` in
[`src/fina/db/schema.py`](../src/fina/db/schema.py). After a migration changes
that revision, old replicas fail readiness on their next check. New replicas
cannot become Ready before their expected schema exists. This MVP therefore
requires an availability gap for schema-changing releases, even with a rolling
update configured to keep two replicas available.

1. Plan a maintenance window for the schema change and its recovery procedure.
2. Add the new revision under `alembic/versions/` and update
   `CURRENT_SCHEMA_REVISION` to match. Keep existing migration history.
3. Build and push an image containing both changes.
4. Deploy that image through the migration hook during the maintenance window;
   Helm waits for the migration before updating the application resources.
5. Deploy the same image through the corporate chart and wait for at least two
   Ready application replicas.

An application-only release with the same schema revision can use the normal
chart rollout. If its pipeline still runs the migration step, Alembic has no
schema changes to apply. Availability during that rollout depends on the chart's
replica count, readiness checks and rolling-update settings.

Rolling back the application image does not undo database migrations. After a
schema change, an older image may fail readiness against the new database.
Handle schema recovery separately; do not run automatic database downgrades as
part of an application rollback.

See the Kubernetes documentation for [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
and [`kubectl wait`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/).

See the [Helm hook lifecycle](https://helm.sh/docs/v3/topics/charts_hooks/).
