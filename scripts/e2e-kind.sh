#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${KIND_CLUSTER_NAME:-mlaiops}"
CONTEXT="kind-${CLUSTER}"
export PATH="${MLOPS_TOOLS_BIN:-$ROOT/.tools.nosync/bin}:$PATH"

command -v kind >/dev/null || {
  echo "kind is not installed. Run 'make kind-up' first." >&2
  exit 1
}
grep -Fxq "$CLUSTER" <<<"$(kind get clusters)" || {
  echo "Kind cluster '${CLUSTER}' does not exist. Run 'make kind-up' first." >&2
  exit 1
}
kubectl config get-contexts "$CONTEXT" >/dev/null 2>&1 || {
  echo "Kubernetes context '${CONTEXT}' does not exist. Run 'make kind-up' first." >&2
  exit 1
}
KUBECTL=(kubectl --context "$CONTEXT")
"${KUBECTL[@]}" cluster-info >/dev/null

"${KUBECTL[@]}" wait --for=condition=Established crd/kiongaagents.mlaiops.io --timeout=90s
"${KUBECTL[@]}" wait --for=condition=Established crd/kiongaworkspaces.mlaiops.io --timeout=90s
"${KUBECTL[@]}" wait --for=condition=Available deployment/mlaiops-operator -n mlaiops-system --timeout=180s

namespace="e2e-$RANDOM"
"${KUBECTL[@]}" create namespace "$namespace"
cleanup() { "${KUBECTL[@]}" delete namespace "$namespace" --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat <<YAML | "${KUBECTL[@]}" apply -f -
apiVersion: mlaiops.io/v1alpha1
kind: KiongaAgent
metadata:
  name: e2e-agent
  namespace: ${namespace}
spec:
  version: "1"
  image: mlaiops/agent-runtime:dev
  graphModule: agents.customer_support.graph:build
  replicas: {min: 1, max: 2}
  llm: {backend: mock}
YAML

"${KUBECTL[@]}" wait --for=condition=Ready kiongaagent/e2e-agent -n "$namespace" --timeout=180s
agent_workload="$("${KUBECTL[@]}" get kiongaagent e2e-agent -n "$namespace" -o jsonpath='{.status.workloadRef}')"
test -n "$agent_workload"
"${KUBECTL[@]}" get deployment "$agent_workload" -n "$namespace"
"${KUBECTL[@]}" get service "$agent_workload" -n "$namespace"
"${KUBECTL[@]}" get horizontalpodautoscaler "$agent_workload" -n "$namespace"

cat <<YAML | "${KUBECTL[@]}" apply -f -
apiVersion: mlaiops.io/v1alpha1
kind: KiongaWorkspace
metadata:
  name: e2e-workspace
  namespace: ${namespace}
spec:
  subject: e2e-user
  services: [workbench, ide]
  compute: {vcpus: 4, memoryGB: 8, maxVMs: 2}
  storageGB: 10
YAML

"${KUBECTL[@]}" wait --for=condition=Ready kiongaworkspace/e2e-workspace -n "$namespace" --timeout=180s
"${KUBECTL[@]}" get deployment e2e-workspace -n "$namespace"
"${KUBECTL[@]}" get service e2e-workspace -n "$namespace"
"${KUBECTL[@]}" get persistentvolumeclaim e2e-workspace -n "$namespace"
