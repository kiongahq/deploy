#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${KIND_CLUSTER_NAME:-kionga}"
CONTEXT="kind-${CLUSTER}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f}"

"$ROOT/scripts/bootstrap-k8s-tools.sh"
export PATH="${MLOPS_TOOLS_BIN:-$ROOT/.tools.nosync/bin}:$PATH"

for command in kind kubectl helm docker; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 1; }
done

docker info >/dev/null || { echo "Docker is not running" >&2; exit 1; }
grep -Fxq "$CLUSTER" <<<"$(kind get clusters)" || kind create cluster \
  --name "$CLUSTER" \
  --config "$ROOT/kind/cluster.yaml" \
  --image "$KIND_NODE_IMAGE"
kubectl config use-context "$CONTEXT" >/dev/null
KUBECTL=(kubectl --context "$CONTEXT")

"${KUBECTL[@]}" create namespace kionga-system --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
"${KUBECTL[@]}" apply -f "$ROOT/config/crd"
"${KUBECTL[@]}" apply -f "$ROOT/config/rbac"

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo add strimzi https://strimzi.io/charts/ >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null
helm repo update >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg --namespace cnpg-system --create-namespace --wait
helm upgrade --install strimzi strimzi/strimzi-kafka-operator --namespace kafka --create-namespace --wait
# Kind node certificates do not contain the node IP SANs used by metrics-server.
# Keep TLS on the API endpoint while allowing metrics-server to scrape those
# local kubelets, so CPU-target agent HPAs work in the fidelity stack.
helm upgrade --install metrics-server metrics-server/metrics-server --namespace kube-system --set 'args[0]=--kubelet-insecure-tls' --wait

# Component sources are sibling checkouts (make workspace).
SRC="${KIONGA_SRC:-$(cd "$ROOT/.." && pwd)}"
for repo in platform trace-proxy agent-runtime sdk-python; do
  [[ -d "$SRC/$repo" ]] || { echo "Missing $SRC/$repo. Run 'make workspace' first." >&2; exit 1; }
done
for service in gateway operator integration-worker feature-gateway storage-proxy metrics-collector; do
  if [[ "$service" == "gateway" ]]; then
    docker build -f "$SRC/platform/Dockerfile.gateway" -t "kionga/$service:dev" "$SRC/platform"
  else
    docker build --build-arg "SERVICE=$service" -t "kionga/$service:dev" "$SRC/platform/go"
  fi
  kind load docker-image --name "$CLUSTER" "kionga/$service:dev"
done
docker build -t kionga/trace-proxy:dev "$SRC/trace-proxy"
kind load docker-image --name "$CLUSTER" kionga/trace-proxy:dev
docker build --build-arg SDK_SOURCE=local --build-context "sdk=$SRC/sdk-python" \
  -t kionga/agent-runtime:dev "$SRC/agent-runtime"
kind load docker-image --name "$CLUSTER" kionga/agent-runtime:dev

"${KUBECTL[@]}" apply -f "$ROOT/config/network"
"${KUBECTL[@]}" apply -f "$ROOT/config/deploy"
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-operator operator=kionga/operator:dev
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-gateway gateway=kionga/gateway:dev
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-integration-worker worker=kionga/integration-worker:dev
"${KUBECTL[@]}" -n kionga-system set env deployment/kionga-operator TRACE_PROXY_IMAGE=kionga/trace-proxy:dev
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-feature-gateway feature-gateway=kionga/feature-gateway:dev
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-storage-proxy storage-proxy=kionga/storage-proxy:dev
"${KUBECTL[@]}" -n kionga-system set image deployment/kionga-metrics-collector collector=kionga/metrics-collector:dev

for target in \
  kionga-operator:operator \
  kionga-gateway:gateway \
  kionga-integration-worker:worker \
  kionga-feature-gateway:feature-gateway \
  kionga-storage-proxy:storage-proxy \
  kionga-metrics-collector:collector; do
  deployment="${target%%:*}"
  container="${target##*:}"
  "${KUBECTL[@]}" -n kionga-system patch deployment "$deployment" --type=strategic \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"'"$container"'","imagePullPolicy":"IfNotPresent"}]}}}}'
done
echo "Core platform applied. Install KFP, KServe, MLflow and Langfuse using your pinned environment values."
