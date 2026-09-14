# fina Kubernetes manifests

Minimal manifest set for the container built from the repo root `Dockerfile`
(see `../README.md`'s "Container deployment"). No Ingress is included here:
ingress class, TLS, and hostnames are cluster/platform-specific. Whatever
Ingress you add, point it only at the `fina` Service (port 80 -> 8000).
Never route to `fina-internal` (port 9000) from outside the cluster --
`/internal/tasks/discovery` has no authentication.

## Commands

Build, test, and push the image (from the repo root):

```bash
docker build --target test -t fina:test . && docker run --rm fina:test
docker build --target runtime -t fina:<tag> .
docker push <registry>/fina:<tag>   # your registry
```

Migrate, then deploy (routine release, no schema change -- see below for a
schema-changing one):

```bash
kubectl apply -f configmap.yaml
kubectl apply -f secret.example.yaml   # after replacing placeholders, or your own equivalent Secrets
kubectl apply -f migration-job.yaml
kubectl wait --for=condition=complete job/fina-migrate-0001-initial-schema --timeout=300s
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
```

Roll out an application-only update (bump the image tag first in
`deployment.yaml`, or use `set image` directly) and check its status:

```bash
kubectl set image deployment/fina fina=<registry>/fina:<new-tag>
kubectl rollout status deployment/fina --timeout=120s
kubectl get pods -l app.kubernetes.io/name=fina
kubectl get rs -l app.kubernetes.io/name=fina   # old ReplicaSet scaled to 0, new one to 2
```

`kubectl rollout undo deployment/fina` reverts to the previous
ReplicaSet if a rollout needs to be backed out (application-only changes
only -- see the maintenance procedure below for anything schema-changing).

### Validated locally (kind)

This manifest set was applied end-to-end against a local `kind` cluster:
image built and `kind load docker-image`'d in, `configmap.yaml` +
hand-created equivalents of the two Secrets applied, `migration-job.yaml`
run to completion, then `deployment.yaml` + `service.yaml` applied and
rolled out (both replicas `Ready`, `startupProbe`/`readinessProbe` passing,
`fina-internal`'s `/probes/ready` and `/metrics` reachable in-cluster,
`fina`'s port correctly requiring auth). A subsequent application-only
rolling update (`kubectl set image` to a re-tagged image, forcing a new
ReplicaSet) was exercised while continuously polling both Services every
0.5s: zero failed requests and zero unready responses across the whole
rollout, confirming `maxUnavailable: 0` / `maxSurge: 1` actually held --
the surge pod became `Ready` before either old pod was terminated. No
`kubectl`/cluster was available in this repo's dev environment beforehand,
so both tools were fetched as plain binaries for this check; a throwaway
in-cluster Postgres Deployment stood in for the external database this
manifest set assumes (not part of the manifests themselves).

## Why the readiness probe makes schema changes special

`/probes/ready` (`fina.db.health.DatabaseProbe`) fails unless the database's
`alembic_version` **exactly** matches the revision baked into the running
image (`fina.db.schema.CURRENT_SCHEMA_REVISION`). This is deliberate and
simple -- no schema-compatibility framework, no version negotiation -- but it
means two revisions can never both be considered "ready" against the same
database at once:

- Migrate first, then roll out the new image: every old pod's readiness
  flips to failing the instant the migration Job completes, all at once --
  not staggered by the rolling update, because they all check the same
  database. The Deployment's `maxUnavailable: 0` cannot help; there's no
  passing pod to route to until the new image is up.
- Roll out the new image first, then migrate: the new pods fail readiness
  (and eventually `startupProbe`, then crash-loop) until the migration Job
  completes, for the same reason in reverse.

Either order produces a real gap between "old schema, old code" and "new
schema, new code." This is an accepted MVP tradeoff, not a bug -- do not
try to paper over it with dual-revision compatibility logic in the app.

## Maintenance procedure for a schema-changing release

1. Announce/expect a brief availability gap; do this in a maintenance
   window if the product needs one.
2. Add the new Alembic revision to `alembic/versions/` as usual (keep
   `0001_initial_schema` as the base -- this procedure is about applying
   new revisions on top of it, not replacing it).
3. Build and push the new image (bumps `CURRENT_SCHEMA_REVISION`).
4. Copy `migration-job.yaml` to a new file, rename the Job
   (`fina-migrate-<new-revision>`), point `image:` at the new tag.
   Never edit and reapply a Job that already ran -- Jobs are immutable;
   give each schema-changing release its own Job object.
5. Apply and wait for that Job to complete.
6. Only then update `deployment.yaml`'s `image:` and apply it. Every
   currently-running pod will fail readiness between step 5 and step 6 for
   the reason above -- this is expected, not a signal to roll back.
7. Confirm both replicas are Ready on the new image before considering the
   release done.

A release with no schema change (application code only, same
`CURRENT_SCHEMA_REVISION`) skips steps 2, 4 and 5 entirely and gets the
normal zero-downtime `maxUnavailable: 0` rollout.

## Secrets

`secret.example.yaml` is a template, not something to apply as-is -- see
its own header comment. The migration Job only ever needs
`fina-database-url` (its container constructs
`fina.config.DatabaseSettings`, never the full `Settings`); never give it
`fina-mcp-api-key`.
