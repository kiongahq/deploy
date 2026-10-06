#!/usr/bin/env bash
set -euo pipefail
[[ $# -ge 3 ]] || { echo "Usage: $0 CONTEXT NAMESPACE BASE.yaml [OVERLAY.yaml ...] [--apply]" >&2; exit 2; }
context="$1" namespace="$2"
shift 2
mode=""
if [[ "${!#}" == --apply ]]; then
  mode=--apply
  set -- "${@:1:$#-1}"
fi
[[ $# -ge 1 ]] || { echo 'At least one values file is required' >&2; exit 2; }
values_args=()
for values in "$@"; do
  [[ "$values" != --* && -f "$values" ]] || { echo "Invalid values file: $values" >&2; exit 2; }
  values_args+=(-f "$values")
done
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v helm >/dev/null
command -v kubectl >/dev/null
helm lint "$root/deploy/helm/kionga" "${values_args[@]}" --namespace "$namespace" --strict
kubectl --context "$context" get namespace "$namespace" >/dev/null
for crd in kiongaagents kiongaworkspaces kiongapipelineruns kiongamodelpromotions kiongatools kiongaconnections; do
  kubectl --context "$context" get crd "$crd.mlaiops.io" >/dev/null
done
rendered="$(helm template kionga "$root/deploy/helm/kionga" --namespace "$namespace" "${values_args[@]}")"
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
  --kube-context "$context" --namespace "$namespace" "${values_args[@]}" \
  --atomic --wait --timeout 15m --history-max 10
kubectl --context "$context" -n "$namespace" rollout status deployment/kionga-gateway --timeout=5m
echo 'Rollout complete. Run the authenticated acceptance checklist before admitting users.'
