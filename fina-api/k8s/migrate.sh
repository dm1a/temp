#!/bin/sh
# Run after configuration is applied, before helm upgrade, in the deploy job.
set -eu

: "${KUBE_CONTEXT:?Set KUBE_CONTEXT to the target deployment cluster}"
: "${NAMESPACE:?Set NAMESPACE to the target deployment namespace}"
: "${FINA_MIGRATION_IMAGE:?Set FINA_MIGRATION_IMAGE to the exact application image being deployed}"

migration_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
migration_manifest=$(mktemp)
trap 'rm -f "$migration_manifest"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Use kubectl's local renderer; do not depend on Python/yq in the Helm runner.
kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" set image --local -f "$migration_dir/migration-job.yaml" \
    "migrate=$FINA_MIGRATION_IMAGE" -o yaml > "$migration_manifest"

# Explicit context and namespace avoid inheriting a different environment.
migration_job=$(kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    create -f "$migration_manifest" -o name)
printf 'Waiting for database migration %s\n' "$migration_job"

if ! kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    wait --for=condition=complete "$migration_job" --timeout=360s; then
    kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
        describe "$migration_job" || true
    kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
        logs "$migration_job" --all-containers=true --prefix=true || true
    printf 'Database migration failed; application rollout must stop.\n' >&2
    exit 1
fi

kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
    logs "$migration_job" --all-containers=true --prefix=true || true
printf 'Database migration completed successfully.\n'
