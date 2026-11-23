#!/usr/bin/env bash
# Install Kionga's cluster prerequisites with pinned versions:
#   cert-manager (wildcard TLS), KEDA core and the KEDA HTTP add-on (request
#   scaling, scale to zero), KServe in Standard (raw) mode with its CRDs and
#   serving runtimes. Optionally Traefik as the ingress controller.
# Idempotent: every step is `helm upgrade --install`.
#
# Usage: helm/prerequisites/install.sh [--kube-context CTX] [--ingress none|traefik]
#                                      [--traefik-values FILE] [--skip NAME]... [--dry-run]
#   --skip takes cert-manager, keda, keda-http, kserve (for a component you already run).
#   --dry-run prints the commands without running them.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"

context="" ingress="none" traefik_values="" dry_run=0 skip=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kube-context) context="$2"; shift 2 ;;
    --ingress) ingress="$2"; shift 2 ;;
    --traefik-values) traefik_values="$2"; shift 2 ;;
    --skip) skip+=("$2"); shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$ingress" in none|traefik) ;; *) echo "--ingress must be none or traefik; ingress-nginx is retired upstream" >&2; exit 2 ;; esac
for name in "${skip[@]+"${skip[@]}"}"; do
  case "$name" in cert-manager|keda|keda-http|kserve) ;; *) echo "--skip takes cert-manager, keda, keda-http or kserve" >&2; exit 2 ;; esac
done
[[ -z "$traefik_values" || -f "$traefik_values" ]] || { echo "No such file: $traefik_values" >&2; exit 2; }

kube=() helm_ctx=()
if [[ -n "$context" ]]; then kube=(--context "$context"); helm_ctx=(--kube-context "$context"); fi

run() {
  echo "+ $*"
  if [[ $dry_run -eq 0 ]]; then "$@"; fi
}
skipped() {
  local name
  for name in "${skip[@]+"${skip[@]}"}"; do [[ "$name" == "$1" ]] && return 0; done
  return 1
}
release() { echo "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/helm-chart-$1-${KSERVE_VERSION}.tgz"; }

if [[ $dry_run -eq 0 ]]; then
  command -v helm >/dev/null || { echo "helm is required" >&2; exit 1; }
  command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
fi

run helm repo add jetstack https://charts.jetstack.io --force-update
run helm repo add kedacore https://kedacore.github.io/charts --force-update
repos=(jetstack kedacore)
if [[ "$ingress" == traefik ]]; then
  run helm repo add traefik https://traefik.github.io/charts --force-update
  repos+=(traefik)
fi
run helm repo update "${repos[@]}"

if [[ "$ingress" == traefik ]]; then
  values=()
  if [[ -n "$traefik_values" ]]; then values=(-f "$traefik_values"); fi
  # allowExternalNameServices: Ingresses for request-scaled workloads point at the
  # KEDA interceptor through an ExternalName Service.
  run helm upgrade --install traefik traefik/traefik "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace ingress-system --create-namespace \
    --version "$TRAEFIK_CHART_VERSION" --set providers.kubernetesIngress.allowExternalNameServices=true \
    --set ingressClass.name=traefik --set ingressClass.isDefaultClass=false "${values[@]+"${values[@]}"}" --wait --timeout 10m
fi

if ! skipped cert-manager; then
  run helm upgrade --install cert-manager jetstack/cert-manager "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace cert-manager --create-namespace \
    --version "$CERT_MANAGER_VERSION" --set crds.enabled=true --wait --timeout 10m
fi

if ! skipped keda; then
  run helm upgrade --install keda kedacore/keda "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace keda --create-namespace \
    --version "$KEDA_VERSION" --wait --timeout 10m
fi

if ! skipped keda-http; then
  run helm upgrade --install keda-add-ons-http kedacore/keda-add-ons-http "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace keda \
    --version "$KEDA_HTTP_ADDON_VERSION" --wait --timeout 10m
fi

if ! skipped kserve; then
  # KServe's webhook certificates come from cert-manager.
  run kubectl "${kube[@]+"${kube[@]}"}" wait --for=condition=Established crd/certificates.cert-manager.io --timeout=120s
  run helm upgrade --install kserve-crd "$(release kserve-crd)" "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace kserve --create-namespace --wait --timeout 10m
  # Standard is KServe's raw mode: plain Deployments, no Knative or Istio.
  # Kionga routes to model servers itself, so KServe creates no Ingress.
  run helm upgrade --install kserve-resources "$(release kserve-resources)" "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace kserve \
    --set kserve.controller.deploymentMode=Standard \
    --set kserve.controller.gateway.disableIngressCreation=true \
    --set kserve.controller.gateway.disableIstioVirtualHost=true --wait --timeout 10m
  run helm upgrade --install kserve-runtime-configs "$(release kserve-runtime-configs)" "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace kserve \
    --set kserve.servingruntime.enabled=true --wait --timeout 10m
fi

echo "Prerequisites installed. Next: a DNS-01 ClusterIssuer for *.<applications.baseDomain> (or a wildcard TLS Secret), then the Kionga chart."
