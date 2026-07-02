#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${KIND_CLUSTER_NAME:-mlaiops}"
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
  --config "$ROOT/deploy/kind/cluster.yaml" \
  --image "$KIND_NODE_IMAGE"
kubectl config use-context "$CONTEXT" >/dev/null
KUBECTL=(kubectl --context "$CONTEXT")

"${KUBECTL[@]}" create namespace mlaiops-system --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
"${KUBECTL[@]}" apply -f "$ROOT/config/crd"
"${KUBECTL[@]}" apply -f "$ROOT/config/rbac"

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo add strimzi https://strimzi.io/charts/ >/dev/null
helm repo update >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg --namespace cnpg-system --create-namespace --wait
helm upgrade --install strimzi strimzi/strimzi-kafka-operator --namespace kafka --create-namespace --wait

for service in gateway operator integration-worker feature-gateway storage-proxy metrics-collector trace-proxy; do
  docker build --build-arg "SERVICE=$service" -t "mlaiops/$service:dev" "$ROOT"
  kind load docker-image --name "$CLUSTER" "mlaiops/$service:dev"
done

"${KUBECTL[@]}" apply -f "$ROOT/config/network"
"${KUBECTL[@]}" apply -f "$ROOT/config/deploy"
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-operator operator=mlaiops/operator:dev
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-gateway gateway=mlaiops/gateway:dev
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-integration-worker worker=mlaiops/integration-worker:dev
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-feature-gateway feature-gateway=mlaiops/feature-gateway:dev
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-storage-proxy storage-proxy=mlaiops/storage-proxy:dev
"${KUBECTL[@]}" -n mlaiops-system set image deployment/mlaiops-metrics-collector collector=mlaiops/metrics-collector:dev

for target in \
  mlaiops-operator:operator \
  mlaiops-gateway:gateway \
  mlaiops-integration-worker:worker \
  mlaiops-feature-gateway:feature-gateway \
  mlaiops-storage-proxy:storage-proxy \
  mlaiops-metrics-collector:collector; do
  deployment="${target%%:*}"
  container="${target##*:}"
  "${KUBECTL[@]}" -n mlaiops-system patch deployment "$deployment" --type=strategic \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"'"$container"'","imagePullPolicy":"IfNotPresent"}]}}}}'
done
echo "Core platform applied. Install KFP, KServe, MLflow and Langfuse using your pinned environment values."
