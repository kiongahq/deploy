#!/usr/bin/env bash
set -euo pipefail
[[ $# -ge 3 && $# -le 4 ]] || { echo "Usage: $0 CONTEXT NAMESPACE VALUES.yaml [--apply]" >&2; exit 2; }
context="$1" namespace="$2" values="$3" mode="${4:-}"
[[ -z "$mode" || "$mode" == --apply ]] || { echo 'Unknown mode' >&2; exit 2; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v helm >/dev/null
command -v kubectl >/dev/null
helm lint "$root/deploy/helm/kionga" -f "$values" --namespace "$namespace" --strict
kubectl --context "$context" get namespace "$namespace" >/dev/null
for crd in kiongaagents kiongaworkspaces kiongapipelineruns kiongamodelpromotions kiongatools kiongaconnections; do
  kubectl --context "$context" get crd "$crd.mlaiops.io" >/dev/null
done
rendered="$(helm template kionga "$root/deploy/helm/kionga" --namespace "$namespace" -f "$values")"
if [[ "$rendered" == *'kind: ExternalSecret'* ]]; then
  kubectl --context "$context" get crd externalsecrets.external-secrets.io >/dev/null
fi
if [[ "$rendered" == *'kind: ServiceMonitor'* ]]; then
  kubectl --context "$context" get crd servicemonitors.monitoring.coreos.com >/dev/null
  kubectl --context "$context" get crd prometheusrules.monitoring.coreos.com >/dev/null
fi
if [[ "$mode" != --apply ]]; then
  echo 'Inputs rendered and cluster prerequisites checked. Add --apply to install/upgrade.'
  exit 0
fi
helm upgrade --install kionga "$root/deploy/helm/kionga" \
  --kube-context "$context" --namespace "$namespace" -f "$values" \
  --atomic --wait --timeout 15m --history-max 10
kubectl --context "$context" -n "$namespace" rollout status deployment/kionga-gateway --timeout=5m
echo 'Rollout complete. Run the authenticated acceptance checklist before admitting users.'
