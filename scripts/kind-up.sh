#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${KIND_CLUSTER_NAME:-mlaiops}"

for command in kind kubectl helm docker; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 1; }
done

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --config "$ROOT/deploy/kind/cluster.yaml"
kubectl create namespace mlaiops-system --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$ROOT/config/crd"
kubectl apply -f "$ROOT/config/rbac"

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo add strimzi https://strimzi.io/charts/ >/dev/null
helm repo update >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg --namespace cnpg-system --create-namespace --wait
helm upgrade --install strimzi strimzi/strimzi-kafka-operator --namespace kafka --create-namespace --wait

for service in gateway operator integration-worker feature-gateway storage-proxy metrics-collector trace-proxy; do
  docker build --build-arg "SERVICE=$service" -t "mlaiops/$service:dev" "$ROOT"
  kind load docker-image --name "$CLUSTER" "mlaiops/$service:dev"
done

kubectl apply -f "$ROOT/config/network"
kubectl apply -f "$ROOT/config/deploy"
kubectl -n mlaiops-system set image deployment/mlaiops-operator operator=mlaiops/operator:dev
kubectl -n mlaiops-system set image deployment/mlaiops-gateway gateway=mlaiops/gateway:dev
kubectl -n mlaiops-system set image deployment/mlaiops-integration-worker worker=mlaiops/integration-worker:dev
echo "Core platform applied. Install KFP, KServe, MLflow and Langfuse using your pinned environment values."
