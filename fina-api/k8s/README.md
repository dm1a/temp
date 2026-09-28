# Database migrations in GitLab

[migrate.sh](migrate.sh) creates [migration-job.yaml](migration-job.yaml) with
the exact application image, waits for `alembic upgrade head` to complete, and
returns a nonzero status if rendering, submission or migration fails. It prints
Job diagnostics on failure. Run it before `helm upgrade` in the deployment job
so a failed migration stops the rollout.

**The helper is not automatically scheduled.** The local `.gitlab-ci.yml`
includes the private `sberinsur/infra-public/common-pipeline` template. Its
expanded deployment job is needed to add the call in the right place while
preserving existing configuration deployment, image selection and rules.

## Connect the GitLab deployment job

In the existing deploy job, apply environment configuration first, select the
application image, run the migration helper, then execute the existing Helm
upgrade and rollout tracking commands. Keep `set -e` enabled and do not suppress
the helper's exit status. Do not put migrations in `after_script`, an app
startup command or an init container.

For the development image naming shown in the GitLab log, insert the following
**after the existing `APP_VERSION` calculation and configuration deployment,
before the application's `helm upgrade`**:

```bash
set -eu
export KUBE_CONTEXT="$K8S_CLUSTER_DEV"
export FINA_MIGRATION_IMAGE="${CR_DEVELOP_URL}/${CI_PROJECT_PATH}:${APP_VERSION}"
sh k8s/migrate.sh
```

`NAMESPACE` is already set in `.gitlab-ci.yml`. The full image reference must
match the image passed to Helm. If the shared template has a variable holding
that reference, use it directly instead of constructing another tag.

Use one `resource_group` for the entire migration-and-deployment job targeting
a given environment, so two pipelines cannot migrate/deploy that database at
the same time. Preserve any existing deployment lock. GitLab documents
[resource groups](https://docs.gitlab.com/ci/resource_groups/) and
[overriding included jobs](https://docs.gitlab.com/ci/yaml/#include).

Do not add a second independent deploy job or override a guessed job name:
that could leave the original deployment running without waiting for migration.
To finish the integration, inspect the existing deploy job in GitLab's expanded
CI configuration, including its name, stage, rules, before_script and script.

## Required cluster configuration

The migration Job uses the same corporate references as the application chart:

| Resource | Required configuration |
| --- | --- |
| `config-fina` ConfigMap | `FINA_DB_HOST`, `FINA_DB_PORT`, `FINA_DB_USER`, `FINA_DB_NAME`, `VAULT_URL`, `VAULT_ENGINE`, `VAULT_SECRET_PATH`. Applied before the migration step. |
| `common-vault` Secret | `VAULT_ROLE_ID_AI_AGENTS` and `VAULT_SECRET_ID_AI_AGENTS`. |
| `harbor` image pull Secret | Access to the application image in the target namespace. |
| Vault secret | A `db_password` key for the application's database role. |

The Job imports `config-fina` with `envFrom`; `DatabaseSettings` ignores keys
unrelated to the database. It resolves the database password from Vault through
the same AppRole as the application, without requiring `FINA_MCP_API_KEY`.
Verify that the Job and application resolve the same database, especially if
`database_url` is also configured and overrides the separate connection fields.
The database role must have permission to apply migrations.

Keep `alembic/` and `alembic.ini` in the application image. The Job retains its
non-root user, read-only filesystem, 300-second execution deadline, two retries
and one-hour cleanup TTL. The helper waits up to 360 seconds, allowing margin
for Kubernetes to report completion. A failed Job may use the full wait timeout.

Kubernetes generates a fresh Job name for every invocation, so a completed Job
cannot be mistaken for a new migration. The helper prints its resource name;
use that name to inspect it before the one-hour cleanup:

```bash
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  describe job/fina-migrate-REPLACE-WITH-GENERATED-NAME
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  logs job/fina-migrate-REPLACE-WITH-GENERATED-NAME --all-containers=true
```

If a pipeline is canceled or the wait times out, the Kubernetes Job may still
be running. Check that its pods have stopped before retrying. Do not bypass a
failed migration to deploy the application. Once the schema is current,
`alembic upgrade head` has no further changes to apply on subsequent rollouts.

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

The shared pipeline include in `.gitlab-ci.yml` does not wire in this
repository's migration Job. Confirm that a migration step succeeded against
the same database before retrying a rollout; creating ConfigMaps and running
Helm alone does not initialize the schema.

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
4. Prepare a migration Job with that image and a fresh name, then run and wait
   for it using the procedure above.
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
