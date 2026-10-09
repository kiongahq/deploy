#!/usr/bin/env bash
# Copy the buckets of a Kionga MinIO volume into the RustFS object store.
#
#   bash scripts/migrate-minio-to-rustfs.sh          (from the deploy checkout)
#   tail -f /tmp/minio-to-rustfs/progress.log         (progress, percent, ETA)
#
# The MinIO volume is only read and is never deleted, so rolling back means
# pointing at it again. MinIO images can no longer be pulled anonymously, so
# this needs the MinIO image already on this machine (MINIO_IMAGE), or any
# MinIO-compatible image you can pull. Re-running resumes: objects already
# copied and verified are skipped. Output: report.csv and summary.json in OUT.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PROJECT="${COMPOSE_PROJECT_NAME:-mlaiops}"
SRC_VOLUME="${MINIO_VOLUME:-${PROJECT}_minio-data}"
MINIO_IMAGE="${MINIO_IMAGE:-minio/minio:RELEASE.2025-07-23T15-54-02Z}"
OUT="${OUT:-/tmp/minio-to-rustfs}"
NETWORK="${PROJECT}_default"
SOURCE="kionga-minio-migration-source"
SRC_USER="${MINIO_ROOT_USER:-mlaiops}"
SRC_PASS="${MINIO_ROOT_PASSWORD:-mlaiops-local-secret}"

fail() { echo "migrate-minio-to-rustfs: $*" >&2; exit 1; }

docker volume inspect "$SRC_VOLUME" >/dev/null 2>&1 || {
  echo "No MinIO volume '$SRC_VOLUME': nothing to migrate."
  exit 0
}
docker image inspect "$MINIO_IMAGE" >/dev/null 2>&1 || fail \
  "MinIO image '$MINIO_IMAGE' is not on this machine and can no longer be pulled anonymously. Set MINIO_IMAGE to a MinIO image you have or can pull (for example cgr.dev/chainguard/minio)."
docker image inspect mlaiops-mlflow >/dev/null 2>&1 || fail "mlaiops-mlflow image missing; run 'make local-up' once first."

echo "==> starting the RustFS object store"
docker compose -f compose.yaml up -d --wait objectstore

echo "==> starting a temporary MinIO on $SRC_VOLUME"
docker rm -f "$SOURCE" >/dev/null 2>&1 || true
docker run -d --rm --name "$SOURCE" --network "$NETWORK" \
  -v "$SRC_VOLUME:/data" -e "MINIO_ROOT_USER=$SRC_USER" -e "MINIO_ROOT_PASSWORD=$SRC_PASS" \
  "$MINIO_IMAGE" server /data >/dev/null
trap 'docker stop "$SOURCE" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 60); do
  docker exec "$SOURCE" curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$SOURCE" curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 \
  || fail "temporary MinIO did not become healthy; see: docker logs $SOURCE"

mkdir -p "$OUT"
echo "==> copying (follow with: tail -f $OUT/progress.log)"
status=0
docker run --rm --network "$NETWORK" \
  -v "$ROOT/scripts/migrate_objects.py:/migrate/migrate_objects.py:ro" -v "$OUT:/out" \
  -e "SOURCE_ENDPOINT=http://$SOURCE:9000" -e "SOURCE_ACCESS_KEY=$SRC_USER" -e "SOURCE_SECRET_KEY=$SRC_PASS" \
  -e "DEST_ENDPOINT=http://objectstore:9000" \
  -e "DEST_ACCESS_KEY=${OBJECTSTORE_ACCESS_KEY:-mlaiops}" \
  -e "DEST_SECRET_KEY=${OBJECTSTORE_SECRET_KEY:-mlaiops-local-secret}" \
  --entrypoint python mlaiops-mlflow /migrate/migrate_objects.py --out /out || status=$?

echo "==> summary ($OUT/summary.json, per-object rows in $OUT/report.csv)"
cat "$OUT/summary.json" 2>/dev/null || true
echo
[[ "$status" -eq 0 ]] || fail "some objects failed verification; see $OUT/report.csv (status=failed). Re-run to retry them."
mkdir -p "$ROOT/.local-state" && date -u +%Y-%m-%dT%H:%M:%SZ > "$ROOT/.local-state/minio-migrated"
echo "Done. $SRC_VOLUME is untouched; remove it yourself once you are satisfied."
