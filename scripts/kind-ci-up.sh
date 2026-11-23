#!/usr/bin/env bash
# Build Kionga from component checkouts and install it on a fresh kind cluster
# the way a customer would: prerequisites, then the chart with digest-pinned
# images. For Linux CI (.github/workflows/kubernetes-e2e.yml); it needs Docker,
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
bash "$ROOT/helm/prerequisites/install.sh" --kube-context "$CONTEXT" --ingress traefik --traefik-values "$ROOT/kind/ci/traefik-values.yaml"

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

step "Kionga secrets"
internal="$(secret)"
database_url="postgres://kionga:${pg_password}@postgres.kionga-data.svc:5432/kionga?sslmode=verify-full&sslrootcert=/etc/kionga/ca/ca.crt"
local_password="$(secret)"
echo "$local_password" >"$OUT/local-password" && chmod 600 "$OUT/local-password"
create() { "${KUBECTL[@]}" -n kionga-system create secret generic "$@" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null; }
create kionga-gateway --from-literal=DATABASE_URL="$database_url" --from-literal=OIDC_CLIENT_SECRET="$(secret)" \
  --from-literal=KIONGA_INTERNAL_TOKEN="$internal" --from-literal=KIONGA_CREDENTIAL_KEY="$(openssl rand -base64 32)" \
  --from-literal=KIONGA_LOCAL_PASSWORD="$local_password"
create kionga-features --from-literal=REDIS_URL=redis://redis.kionga-data.svc:6379/0 --from-literal=KIONGA_INTERNAL_TOKEN="$internal"
create kionga-storage --from-literal=S3_ACCESS_KEY="$access_key" --from-literal=S3_SECRET_KEY="$secret_key" --from-literal=KIONGA_INTERNAL_TOKEN="$internal"
trace_key="$(secret)"
trace_hash="$(printf %s "$trace_key" | openssl dgst -sha256 -r | cut -d' ' -f1)"
create kionga-tracing --from-literal=TRACE_PROXY_KEYS="[{\"key_id\":\"e2e\",\"key_sha256\":\"$trace_hash\",\"project\":\"e2e\"}]"

step "Kionga chart"
helm upgrade --install kionga "$ROOT/helm/kionga" --kube-context "$CONTEXT" --namespace kionga-system \
  -f "$ROOT/kind/ci/values.yaml" -f "$OUT/images.yaml" --timeout 10m
"${KUBECTL[@]}" -n kionga-workloads create secret generic kionga-model-storage \
  --from-literal=AWS_ACCESS_KEY_ID="$access_key" --from-literal=AWS_SECRET_ACCESS_KEY="$secret_key" \
  --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
for deployment in kionga-operator kionga-gateway; do
  "${KUBECTL[@]}" -n kionga-system rollout status "deployment/$deployment" --timeout=10m
done
"${KUBECTL[@]}" -n kionga-system get pods -o wide
echo "Kionga is installed on $CONTEXT. Gateway: https://kionga.127.0.0.1.sslip.io (CA: $OUT/ca.crt)"
