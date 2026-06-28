#!/usr/bin/env bash
set -euo pipefail

kubectl wait --for=condition=Established crd/nexusagents.mlaiops.io --timeout=90s
kubectl wait --for=condition=Available deployment/mlaiops-operator -n mlaiops-system --timeout=180s

namespace="e2e-$RANDOM"
kubectl create namespace "$namespace"
cleanup() { kubectl delete namespace "$namespace" --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat <<YAML | kubectl apply -f -
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

kubectl wait --for=condition=Ready nexusagent/e2e-agent -n "$namespace" --timeout=180s
kubectl get deployment e2e-agent-1 -n "$namespace"
kubectl get service e2e-agent-1 -n "$namespace"
