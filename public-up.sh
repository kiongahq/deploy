#!/usr/bin/env bash
# One-command public deployment: the local stack plus TLS + OIDC edge.
# Run on the VM from the repository root:  ./deploy/public-up.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "error: .env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi
set -a; source .env; set +a
for required in MLAIOPS_DOMAIN MLAIOPS_ACME_EMAIL DEX_CLIENT_SECRET DEX_ADMIN_EMAIL DEX_ADMIN_PASSWORD_HASH MLAIOPS_INTERNAL_TOKEN; do
  if [[ -z "${!required:-}" ]]; then
    echo "error: $required is not set in .env" >&2
    exit 1
  fi
done

compose=(docker compose -f deploy/compose.yaml -f deploy/compose.public.yaml)

echo "==> validating configuration"
"${compose[@]}" config --quiet

echo "==> building platform images"
"${compose[@]}" build

echo "==> starting the stack"
"${compose[@]}" up -d

echo "==> creating Kafka topics"
KAFKA_BROKER=localhost:9092 ./scripts/local-topics.sh || true

echo "==> waiting for the gateway"
for _ in $(seq 1 60); do
  if "${compose[@]}" exec -T caddy wget -q -O /dev/null "http://gateway:8080/api/v1/health"; then
    echo "==> platform is up: https://${MLAIOPS_DOMAIN}"
    exit 0
  fi
  sleep 2
done
echo "error: gateway did not become healthy; check 'docker compose logs gateway'" >&2
exit 1
