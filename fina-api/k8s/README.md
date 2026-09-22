# Database migration Job

This directory contains [migration-job.yaml](migration-job.yaml), which runs
`alembic upgrade head` before an application rollout. Application deployment,
Services, configuration and credential references are managed by the corporate
pipeline using [`.chart/`](../.chart/).

The pipeline must explicitly create this Job and wait for it to succeed. Having
the manifest in the repository does not make the chart run it automatically.
Run migrations as a separate deployment step, never in each application's
startup command or init container. Serialize deployments targeting the same
database so two releases cannot migrate it concurrently.

## Prepare the Job

Before submitting the manifest, set these values manually or through the pipeline:

| Setting | Required value |
| --- | --- |
| `metadata.name` | A fresh name for each deployment attempt, for example `fina-migrate-0001-initial-schema-release-123`. |
| Container `image` | The exact registry image tag or digest being deployed to the application; replace `fina:latest`. |
| Namespace | The target environment's namespace, supplied to `kubectl` below. |
| `FINA_DATABASE_URL` Secret reference | An existing Secret in that namespace; the manifest defaults to Secret `fina-database-url`, key `url`. |
| Pod `imagePullSecrets` / service account | The corporate registry access required to pull the image, either configured on the Job or supplied by the platform. |

The Job must reach the same PostgreSQL database as the application, using an
account with permission to apply the migrations. Provision the database Secret
through the corporate secret-management process before creating the Job; do not
commit credentials. Its `url` value uses the `postgresql+asyncpg://...` format.

The migration process constructs `DatabaseSettings` from
[`src/fina/config.py`](../src/fina/config.py). It needs only
`FINA_DATABASE_URL`; do not inject `FINA_MCP_API_KEY`, `VAULT_*`, or the full
application environment. If the database credential is stored in Vault, the
platform must make it available through the referenced Kubernetes Secret.

Keep [`alembic/`](../alembic/) and [`alembic.ini`](../alembic.ini) in the image.
The existing Job uses `restartPolicy: Never`, allows two retries, has a
300-second execution deadline, and is eligible for cleanup one hour after it
finishes. Preserve its non-root and read-only filesystem settings.

## Run migrations before deployment

Run this as a pipeline shell step from the repository root after preparing the
manifest. Replace the namespace below and set `FINA_MIGRATION_JOB` to exactly the
`metadata.name` in that manifest. These shell variables do not change the YAML.

```bash
set -eu

FINA_DEPLOY_NAMESPACE="replace-with-target-namespace"
FINA_MIGRATION_JOB="fina-migrate-0001-initial-schema-release-123"

kubectl --namespace "$FINA_DEPLOY_NAMESPACE" create -f k8s/migration-job.yaml
kubectl --namespace "$FINA_DEPLOY_NAMESPACE" wait \
  --for=condition=complete "job/$FINA_MIGRATION_JOB" --timeout=360s
```

Use `create` with a fresh Job name so an existing completed Job cannot be
mistaken for a new successful migration. The wait timeout allows some margin
beyond the Job's 300-second deadline. A failed Job cannot satisfy this wait;
the command may wait until its timeout before returning failure.

Configure the application deployment stage to run only if this step succeeds.
Then deploy the same image through the corporate chart and confirm that at
least two application replicas become Ready. A pipeline rerun needs a fresh
Job name; `alembic upgrade head` has nothing to apply when that image's target
schema is already current.

For a failed or timed-out migration, stop the rollout and inspect the Job:

```bash
kubectl --namespace "$FINA_DEPLOY_NAMESPACE" describe "job/$FINA_MIGRATION_JOB"
kubectl --namespace "$FINA_DEPLOY_NAMESPACE" logs \
  --selector "job-name=$FINA_MIGRATION_JOB" --all-containers=true --prefix=true
```

Collect diagnostics before the one-hour cleanup removes the Job and its pods.
Check that the previous Job's pods have stopped before submitting another
attempt. Correct the cause and use a fresh name; do not bypass the migration
failure to deploy the application.

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
