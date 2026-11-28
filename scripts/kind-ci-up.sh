#!/usr/bin/env bash
# Build Kionga from component checkouts and install it on a fresh kind cluster
# the way a customer would: prerequisites, OpenBao bootstrap and seeding, then
# the chart with digest-pinned images. Every platform Secret comes from OpenBao
# through External Secrets Operator; only the test databases in kionga-data get
# kubectl-created Secrets. For Linux CI (.github/workflows/kubernetes-e2e.yml); it needs Docker,
# kind, kubectl, helm and about 20 GB of disk. Images go to a registry container
# on this machine only (localhost:5001), never to a public registry.
#
# Environment:
#   KIONGA_SRC   folder with platform, trace-proxy, agent-runtime, sdk-python checkouts (default: ..)
#   OUT          where generated values and logs go (default: $RUNNER_TEMP or /tmp/kionga-e2e)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${KIONGA_SRC:-$(cd "$ROOT/.." && pwd)}"
OUT="${OUT:-${RUNNER_TEMP:-/tmp}/kionga-e2e}"
CLUSTER=kionga-e2e
CONTEXT="kind-$CLUSTER"
REGISTRY_NAME=kind-registry
REGISTRY=localhost:5001
REGISTRY_IMAGE="registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f}"
# Images the chart must name but this lane never runs (workspaces, Langflow).
UNUSED_IMAGE="registry.k8s.io/pause:3.10@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a"
KUBECTL=(kubectl --context "$CONTEXT")
mkdir -p "$OUT"

step() { printf '\n== %s (%s)\n' "$1" "$(date -u +%H:%M:%S)"; }
BOOTSTRAP=(bash "$ROOT/scripts/openbao-bootstrap.sh")
for tool in docker kind kubectl helm openssl python3; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done
for repo in platform trace-proxy agent-runtime sdk-python; do
  [[ -d "$SRC/$repo" ]] || { echo "Missing $SRC/$repo" >&2; exit 1; }
done

step "Local registry and kind cluster"
if [[ "$(docker inspect -f '{{.State.Running}}' "$REGISTRY_NAME" 2>/dev/null || true)" != true ]]; then
  docker run -d --restart=always -p "127.0.0.1:5001:5000" --name "$REGISTRY_NAME" "$REGISTRY_IMAGE" >/dev/null
fi
kind get clusters | grep -Fxq "$CLUSTER" || kind create cluster --name "$CLUSTER" --config "$ROOT/kind/ci/cluster.yaml" --image "$KIND_NODE_IMAGE" --wait 5m
for node in $(kind get nodes --name "$CLUSTER"); do
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/$REGISTRY"
  printf '[host."http://%s:5000"]\n' "$REGISTRY_NAME" | docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/$REGISTRY/hosts.toml"
done
docker network connect kind "$REGISTRY_NAME" 2>/dev/null || true

step "Build images from source and push them to the local registry"
declare -A IMAGES
build() { # build NAME CONTEXT [docker build args...]
  local name="$1" context="$2"; shift 2
  docker build --quiet -t "$REGISTRY/kionga/$name:e2e" "$@" "$context" >/dev/null
  docker push --quiet "$REGISTRY/kionga/$name:e2e" >/dev/null
  IMAGES[$name]="$(docker inspect --format '{{index .RepoDigests 0}}' "$REGISTRY/kionga/$name:e2e")"
  echo "$name -> ${IMAGES[$name]}"
}
# The gateway can come from a different platform ref (platform-gateway checkout).
GATEWAY_SRC="$SRC/platform"
[[ -d "$SRC/platform-gateway" ]] && GATEWAY_SRC="$SRC/platform-gateway"
build gateway "$GATEWAY_SRC" -f "$GATEWAY_SRC/Dockerfile.gateway"
for service in operator integration-worker feature-gateway storage-proxy; do
  build "$service" "$SRC/platform/go" --build-arg "SERVICE=$service"
done
build trace-proxy "$SRC/trace-proxy"
build agent-runtime "$SRC/agent-runtime" --build-arg SDK_SOURCE=local --build-context "sdk=$SRC/sdk-python"
build mlflow "$ROOT/mlflow"
{
  echo "images:"
  for name in gateway operator integration-worker feature-gateway storage-proxy trace-proxy agent-runtime; do
    echo "  $name: ${IMAGES[$name]}"
  done
  for name in jupyter ide langflow langflow-sidecar langflow-agent-runtime; do
    echo "  $name: $UNUSED_IMAGE"
  done
} >"$OUT/images.yaml"
printf 'agent_runtime=%s\nmlflow=%s\n' "${IMAGES[agent-runtime]}" "${IMAGES[mlflow]}" >"$OUT/images.env"

step "Prerequisites"
bash "$ROOT/helm/prerequisites/install.sh" --kube-context "$CONTEXT" --ingress traefik --traefik-values "$ROOT/kind/ci/traefik-values.yaml" \
  --openbao-values "$ROOT/kind/ci/openbao-values.yaml"

step "OpenBao: initialize, unseal, configure"
# The key shares stay on the runner (never in the cluster, never uploaded).
"${BOOTSTRAP[@]}" all --kube-context "$CONTEXT" --key-file "$OUT/openbao-init.json" --ca-out "$OUT/openbao-ca.crt"
# A rerun must change nothing.
"${BOOTSTRAP[@]}" all --kube-context "$CONTEXT" --key-file "$OUT/openbao-init.json" 2>"$OUT/openbao-rerun.log"
if grep -E "written|enabled|created|initialized:" "$OUT/openbao-rerun.log"; then
  echo "openbao-bootstrap.sh changed state on a rerun" >&2; exit 1
fi
"${BOOTSTRAP[@]}" status --kube-context "$CONTEXT"

step "Test dependencies: CA, PostgreSQL with TLS, object store, MLflow"
secret() { openssl rand -hex 24; }
pg_password="$(secret)" access_key="$(secret)" secret_key="$(secret)"
sed "s|MLFLOW_IMAGE|${IMAGES[mlflow]}|" "$ROOT/kind/ci/fixtures.yaml" >"$OUT/fixtures.yaml"
# Namespaces first so the Secrets below have somewhere to go.
for namespace in kionga-system kionga-data; do
  "${KUBECTL[@]}" create namespace "$namespace" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
done
"${KUBECTL[@]}" create secret generic postgres -n kionga-data --from-literal=password="$pg_password" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
"${KUBECTL[@]}" create secret generic objectstore -n kionga-data --from-literal=access-key="$access_key" --from-literal=secret-key="$secret_key" \
  --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
"${KUBECTL[@]}" apply -f "$OUT/fixtures.yaml"
"${KUBECTL[@]}" -n cert-manager wait --for=condition=Ready certificate/kionga-e2e-ca --timeout=180s
"${KUBECTL[@]}" -n kionga-system wait --for=condition=Ready certificate/kionga-tls --timeout=180s
"${KUBECTL[@]}" -n kionga-data wait --for=condition=Ready certificate/postgres-tls --timeout=180s
"${KUBECTL[@]}" -n cert-manager get secret kionga-e2e-ca -o jsonpath='{.data.ca\.crt}' | base64 -d >"$OUT/ca.crt"
"${KUBECTL[@]}" -n kionga-system create configmap kionga-e2e-ca --from-file=ca.crt="$OUT/ca.crt" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
for deployment in postgres objectstore mlflow; do
  "${KUBECTL[@]}" -n kionga-data rollout status "deployment/$deployment" --timeout=10m
done
sed "s|MLFLOW_IMAGE|${IMAGES[mlflow]}|" "$ROOT/kind/ci/register-model.yaml" | "${KUBECTL[@]}" apply -f -
"${KUBECTL[@]}" -n kionga-data wait --for=condition=Complete job/register-model --timeout=10m

step "Kionga secrets in OpenBao"
# Values derived from the test databases go in as files; the rest are random
# values OpenBao keeps. Nothing is printed and nothing becomes a kubectl Secret.
seed_dir="$OUT/openbao-seed"
( umask 077
  mkdir -p "$seed_dir"/{gateway,features,storage,tracing,model-storage}
  printf '%s' "postgres://kionga:${pg_password}@postgres.kionga-data.svc:5432/kionga?sslmode=verify-full&sslrootcert=/etc/kionga/ca/ca.crt" \
    >"$seed_dir/gateway/DATABASE_URL"
  secret >"$OUT/local-password"
  cp "$OUT/local-password" "$seed_dir/gateway/KIONGA_LOCAL_PASSWORD"
  printf '%s' redis://redis.kionga-data.svc:6379/0 >"$seed_dir/features/REDIS_URL"
  printf '%s' "$access_key" >"$seed_dir/storage/S3_ACCESS_KEY"
  printf '%s' "$secret_key" >"$seed_dir/storage/S3_SECRET_KEY"
  printf '%s' "$access_key" >"$seed_dir/model-storage/AWS_ACCESS_KEY_ID"
  printf '%s' "$secret_key" >"$seed_dir/model-storage/AWS_SECRET_ACCESS_KEY"
  trace_hash="$(secret | openssl dgst -sha256 -r | cut -d' ' -f1)"
  printf '[{"key_id":"e2e","key_sha256":"%s","project":"e2e"}]' "$trace_hash" >"$seed_dir/tracing/TRACE_PROXY_KEYS" )
"${BOOTSTRAP[@]}" seed --kube-context "$CONTEXT" --key-file "$OUT/openbao-init.json" --from-dir "$seed_dir" \
  --generate shared/KIONGA_INTERNAL_TOKEN=hex:24 --generate gateway/OIDC_CLIENT_SECRET=hex:24 \
  --generate gateway/KIONGA_CREDENTIAL_KEY=base64:32 --generate gateway/KIONGA_SIGNING_KEY=base64:32
find "$seed_dir" -type f -delete
find "$seed_dir" -depth -type d -empty -delete

step "Kionga chart"
helm upgrade --install kionga "$ROOT/helm/kionga" --kube-context "$CONTEXT" --namespace kionga-system \
  -f "$ROOT/kind/ci/values.yaml" -f "$OUT/images.yaml" --set-file "secretStore.openbao.caBundle=$OUT/openbao-ca.crt" --timeout 10m
for namespace in kionga-system kionga-workloads; do
  "${KUBECTL[@]}" -n "$namespace" wait --for=condition=Ready secretstore/kionga-store --timeout=180s
  "${KUBECTL[@]}" -n "$namespace" wait --for=condition=Ready externalsecret --all --timeout=180s
done
for deployment in kionga-operator kionga-gateway; do
  "${KUBECTL[@]}" -n kionga-system rollout status "deployment/$deployment" --timeout=10m
done

step "Preflight against the live store"
# A Ready store passes; a store that does not exist stops the upgrade.
helm upgrade kionga "$ROOT/helm/kionga" --kube-context "$CONTEXT" --namespace kionga-system --dry-run=server \
  -f "$ROOT/kind/ci/values.yaml" -f "$OUT/images.yaml" --set-file "secretStore.openbao.caBundle=$OUT/openbao-ca.crt" >/dev/null
if helm upgrade kionga "$ROOT/helm/kionga" --kube-context "$CONTEXT" --namespace kionga-system --dry-run=server \
    -f "$ROOT/kind/ci/values.yaml" -f "$OUT/images.yaml" --set secretStore.openbao.enabled=false --set externalSecrets.workspaceTargets=null \
    --set externalSecrets.storeName=kionga-missing-store >/dev/null 2>"$OUT/preflight-missing.log"; then
  echo "the preflight accepted a SecretStore that does not exist" >&2; exit 1
fi
grep -q "kionga-missing-store, which does not exist" "$OUT/preflight-missing.log" || { cat "$OUT/preflight-missing.log" >&2; exit 1; }
"${KUBECTL[@]}" -n kionga-system get pods -o wide
echo "Kionga is installed on $CONTEXT. Gateway: https://kionga.127.0.0.1.sslip.io (CA: $OUT/ca.crt)"
