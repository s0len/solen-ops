#!/usr/bin/env bash
# Migrate Plex config subdirectories from the backed-up `plex` PVC to the new
# `plex-scratch` PVC. Excludes Metadata (irreplaceable custom posters) and Cache
# (already on its own PVC).
#
# Prerequisite: pvc.yaml + helmrelease.yaml changes are committed AND pushed,
# and Flux has reconciled the kustomization at least once so the new
# `plex-scratch` PVC exists.
#
# Run from repo root with KUBECONFIG set (mise handles this).

set -euo pipefail

NS="media"
APP="plex"
SRC_PVC="plex"
DST_PVC="plex-scratch"
MIG_POD="plex-scratch-migrate"

DIRS=(
  "Media"
  "Drivers"
  "Codecs"
  "Scanners"
  "Crash Reports:CrashReports"
)

echo "==> Suspending HelmRelease ${APP} in namespace ${NS}"
flux suspend helmrelease -n "${NS}" "${APP}"

echo "==> Reconciling kustomization to ensure ${DST_PVC} PVC exists"
flux reconcile kustomization -n flux-system "${APP}" || true

echo "==> Waiting for PVC ${DST_PVC} to be created (up to 2 min)"
kubectl wait --for=jsonpath='{.status.phase}'=Bound \
  -n "${NS}" "pvc/${DST_PVC}" --timeout=120s

echo "==> Scaling deployment/${APP} to 0"
kubectl scale -n "${NS}" "deployment/${APP}" --replicas=0
kubectl wait --for=delete -n "${NS}" pod -l "app.kubernetes.io/name=${APP}" --timeout=300s || true

echo "==> Removing any prior migration pod"
kubectl delete pod -n "${NS}" "${MIG_POD}" --ignore-not-found --wait=true

echo "==> Applying migration pod"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${MIG_POD}
  namespace: ${NS}
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 0
    runAsGroup: 0
    fsGroup: 0
  containers:
    - name: migrate
      image: alpine:3.20
      command: ["sleep", "3600"]
      volumeMounts:
        - name: src
          mountPath: /src
        - name: dst
          mountPath: /dst
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
        limits:
          cpu: "2"
          memory: 1Gi
  volumes:
    - name: src
      persistentVolumeClaim:
        claimName: ${SRC_PVC}
    - name: dst
      persistentVolumeClaim:
        claimName: ${DST_PVC}
EOF

echo "==> Waiting for migration pod to be Ready"
kubectl wait --for=condition=Ready -n "${NS}" "pod/${MIG_POD}" --timeout=300s

PMS_PATH="/src"

echo "==> Source contents (top-level sizes):"
kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sh '${PMS_PATH}'/* 2>/dev/null || true"

for entry in "${DIRS[@]}"; do
  src_name="${entry%%:*}"
  dst_name="${entry##*:}"
  if [ "${src_name}" = "${entry}" ]; then
    dst_name="${src_name}"
  fi

  src_path="${PMS_PATH}/${src_name}"
  dst_path="/dst/${dst_name}"

  echo
  echo "==> Migrating '${src_name}' -> ${DST_PVC}:/${dst_name}"

  if ! kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "test -d \"${src_path}\""; then
    echo "    Source '${src_path}' does not exist, skipping."
    continue
  fi

  if kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "test -d \"${dst_path}\" && [ -n \"\$(ls -A \"${dst_path}\" 2>/dev/null)\" ]"; then
    echo "    Destination '${dst_path}' already populated, skipping copy (idempotent)."
  else
    kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "mkdir -p \"${dst_path}\" && cp -a \"${src_path}\"/. \"${dst_path}\"/"
  fi

  src_size=$(kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sb \"${src_path}\" | awk '{print \$1}'")
  dst_size=$(kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sb \"${dst_path}\" | awk '{print \$1}'")
  src_human=$(kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sh \"${src_path}\" | awk '{print \$1}'")
  dst_human=$(kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sh \"${dst_path}\" | awk '{print \$1}'")
  echo "    src=${src_human} (${src_size} bytes)  dst=${dst_human} (${dst_size} bytes)"

  if [ "${src_size}" != "${dst_size}" ]; then
    echo "    Size mismatch for '${src_name}', aborting before deleting source."
    exit 1
  fi

  echo "    Sizes match. Removing source '${src_path}'."
  kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "rm -rf \"${src_path}\""
done

echo
echo "==> Final source layout (Metadata + Cache should remain):"
kubectl exec -n "${NS}" "${MIG_POD}" -- sh -c "du -sh '${PMS_PATH}'/* 2>/dev/null || true"

echo "==> Deleting migration pod"
kubectl delete pod -n "${NS}" "${MIG_POD}" --wait=true

echo "==> Resuming HelmRelease"
flux resume helmrelease -n "${NS}" "${APP}"

echo "==> Waiting for plex pod to become Ready"
kubectl rollout status -n "${NS}" "deployment/${APP}" --timeout=600s
kubectl wait --for=condition=Ready -n "${NS}" pod -l "app.kubernetes.io/name=${APP}" --timeout=600s

echo "==> Triggering manual VolSync sync(s)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mapfile -t RSRCS < <(kubectl get replicationsource -n "${NS}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -E "^${APP}(-|$)" || true)
if [ "${#RSRCS[@]}" -eq 0 ]; then
  echo "    No ReplicationSource matching '^${APP}' found in namespace ${NS}."
else
  for rs in "${RSRCS[@]}"; do
    echo "    Annotating replicationsource/${rs} to trigger sync"
    kubectl annotate replicationsource -n "${NS}" "${rs}" \
      "volsync.backube/manual=${NOW}" --overwrite
  done
fi

echo
echo "==> Done. Verify in Plex that libraries open, posters/art render, and"
echo "    that the most recent VolSync snapshot completes (kubectl get replicationsource -n ${NS})."
