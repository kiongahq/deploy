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

"${KUBECTL[@]}" wait --for=condition=Established crd/nexusagents.mlaiops.io --timeout=90s
"${KUBECTL[@]}" wait --for=condition=Available deployment/mlaiops-operator -n mlaiops-system --timeout=180s

namespace="e2e-$RANDOM"
"${KUBECTL[@]}" create namespace "$namespace"
cleanup() { "${KUBECTL[@]}" delete namespace "$namespace" --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat <<YAML | "${KUBECTL[@]}" apply -f -
apiVersion: mlaiops.io/v1alpha1
kind: NexusAgent
metadata:
  name: e2e-agent
  namespace: ${namespace}
spec:
  version: "1"
  image: nginx:1.27-alpine
  graphModule: agents.e2e:graph
  replicas: {min: 1, max: 2}
  llm: {backend: self-hosted}
YAML

"${KUBECTL[@]}" wait --for=condition=Ready nexusagent/e2e-agent -n "$namespace" --timeout=180s
"${KUBECTL[@]}" get deployment e2e-agent-1 -n "$namespace"
"${KUBECTL[@]}" get service e2e-agent-1 -n "$namespace"
