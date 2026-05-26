#!/usr/bin/env bash
# Clean up stale Plex database artifacts inside the running plex pod:
#   - *.bak / *.new files (leftover migration files from March 2025, ~2.4 GB)
#   - All dated backup files matching *-YYYY-MM-DD EXCEPT the most recent date (~5 GB)
#
# Safe-by-construction: never touches live `*.db`, `*.db-wal`, `*.db-shm`, or
# active `*.blobs.db` files. The dated-glob uses `*-20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]`
# so suffixes like `blobs.db-wal` (no date) are NOT matched.
#
# Run while Plex is up and idle. Best to stop active streams first.

set -euo pipefail

NS="media"
APP="plex"
DB_DIR="/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases"

POD="$(kubectl get pod -n "${NS}" -l "app.kubernetes.io/name=${APP}" -o jsonpath='{.items[0].metadata.name}')"
if [ -z "${POD}" ]; then
  echo "No plex pod found in namespace ${NS}" >&2
  exit 1
fi
echo "==> Using pod: ${POD}"

echo "==> Before:"
kubectl exec -n "${NS}" "${POD}" -- sh -c "du -sh \"${DB_DIR}\" && ls -lah \"${DB_DIR}\""

echo
echo "==> Deleting *.bak and *.new files"
kubectl exec -n "${NS}" "${POD}" -- sh -c "
  set -eu
  cd \"${DB_DIR}\"
  for f in *.bak *.new; do
    [ -e \"\$f\" ] || continue
    echo \"  rm \$f\"
    rm -f -- \"\$f\"
  done
"

echo
echo "==> Identifying dated backup files (pattern: *-YYYY-MM-DD)"
# List dated files, extract the date suffix, find the newest date, then delete
# every dated file whose date suffix is not the newest.
kubectl exec -n "${NS}" "${POD}" -- sh -c "
  set -eu
  cd \"${DB_DIR}\"
  # Enumerate dated files (glob match avoids accidental wal/shm matches).
  files=\$(ls -1 2>/dev/null | grep -E '.*-20[0-9]{2}-[0-9]{2}-[0-9]{2}\$' || true)
  if [ -z \"\$files\" ]; then
    echo '  No dated backup files found.'
    exit 0
  fi

  # Find newest date suffix (lexicographic sort works for YYYY-MM-DD).
  newest=\$(echo \"\$files\" | sed -E 's/.*-(20[0-9]{2}-[0-9]{2}-[0-9]{2})\$/\1/' | sort -u | tail -n1)
  echo \"  Newest dated suffix: \$newest (keeping)\"

  echo \"\$files\" | while IFS= read -r f; do
    [ -n \"\$f\" ] || continue
    suffix=\$(echo \"\$f\" | sed -E 's/.*-(20[0-9]{2}-[0-9]{2}-[0-9]{2})\$/\1/')
    if [ \"\$suffix\" = \"\$newest\" ]; then
      echo \"  keep  \$f\"
    else
      echo \"  rm    \$f\"
      rm -f -- \"\$f\"
    fi
  done
"

echo
echo "==> After:"
kubectl exec -n "${NS}" "${POD}" -- sh -c "du -sh \"${DB_DIR}\" && ls -lah \"${DB_DIR}\""

echo
echo "==> Done."
