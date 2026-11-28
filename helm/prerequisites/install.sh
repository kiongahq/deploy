#!/usr/bin/env bash
# Install Kionga's cluster prerequisites with pinned versions:
#   cert-manager (wildcard TLS), KEDA core and the KEDA HTTP add-on (request
#   scaling, scale to zero), KServe in Standard (raw) mode with its CRDs and
#   serving runtimes, and secrets: OpenBao (three Raft servers, TLS from
#   cert-manager), External Secrets Operator and Reloader (rolls Kionga pods in
#   the release namespace when a Secret they mount changes). Optionally Traefik
#   as the ingress controller.
# Idempotent: every step is `helm upgrade --install` or `kubectl apply`.
#
# Usage: helm/prerequisites/install.sh [--kube-context CTX] [--ingress none|traefik]
#          [--traefik-values FILE] [--openbao-values FILE] [--kionga-namespace NS]
#          [--skip NAME]... [--dry-run]
#   --skip takes cert-manager, keda, keda-http, kserve, openbao, external-secrets,
#     reloader (for a component you already run; skip openbao to use your own).
#   --openbao-values adds a values file after helm/prerequisites/openbao-values.yaml.
#   --kionga-namespace is the Kionga release namespace Reloader watches (default kionga-system).
#   --dry-run prints the commands without running them.
# OpenBao starts sealed: run scripts/openbao-bootstrap.sh next.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"

context="" ingress="none" traefik_values="" openbao_values="" kionga_namespace="kionga-system" dry_run=0 skip=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kube-context) context="$2"; shift 2 ;;
    --ingress) ingress="$2"; shift 2 ;;
    --traefik-values) traefik_values="$2"; shift 2 ;;
    --openbao-values) openbao_values="$2"; shift 2 ;;
    --kionga-namespace) kionga_namespace="$2"; shift 2 ;;
    --skip) skip+=("$2"); shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$ingress" in none|traefik) ;; *) echo "--ingress must be none or traefik; ingress-nginx is retired upstream" >&2; exit 2 ;; esac
for name in "${skip[@]+"${skip[@]}"}"; do
  case "$name" in cert-manager|keda|keda-http|kserve|openbao|external-secrets|reloader) ;;
    *) echo "--skip takes cert-manager, keda, keda-http, kserve, openbao, external-secrets or reloader" >&2; exit 2 ;; esac
done
[[ -z "$traefik_values" || -f "$traefik_values" ]] || { echo "No such file: $traefik_values" >&2; exit 2; }
[[ -z "$openbao_values" || -f "$openbao_values" ]] || { echo "No such file: $openbao_values" >&2; exit 2; }
[[ "$kionga_namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || { echo "--kionga-namespace must be a namespace name" >&2; exit 2; }

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
if ! skipped openbao; then
  run helm repo add openbao https://openbao.github.io/openbao-helm --force-update
  repos+=(openbao)
fi
if ! skipped external-secrets; then
  run helm repo add external-secrets https://charts.external-secrets.io --force-update
  repos+=(external-secrets)
fi
if ! skipped reloader; then
  run helm repo add stakater https://stakater.github.io/stakater-charts --force-update
  repos+=(stakater)
fi
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

if ! skipped external-secrets; then
  run helm upgrade --install external-secrets external-secrets/external-secrets "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace external-secrets \
    --create-namespace --version "$EXTERNAL_SECRETS_VERSION" --set installCRDs=true --wait --timeout 10m
fi

if ! skipped openbao; then
  # The namespace and the server certificate from cert-manager; the chart mounts its Secret.
  run kubectl "${kube[@]+"${kube[@]}"}" wait --for=condition=Established crd/certificates.cert-manager.io --timeout=120s
  run kubectl "${kube[@]+"${kube[@]}"}" apply -f "$here/openbao-tls.yaml"
  run kubectl "${kube[@]+"${kube[@]}"}" -n openbao wait --for=condition=Ready certificate/openbao-server-tls --timeout=180s
  values=(-f "$here/openbao-values.yaml")
  if [[ -n "$openbao_values" ]]; then values+=(-f "$openbao_values"); fi
  # No --wait: the servers stay unready until openbao-bootstrap.sh unseals them.
  run helm upgrade --install openbao openbao/openbao "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace openbao --create-namespace \
    --version "$OPENBAO_CHART_VERSION" "${values[@]}" --set "server.image.tag=${OPENBAO_VERSION}@${OPENBAO_IMAGE_DIGEST}" --timeout 10m
fi

if ! skipped reloader; then
  # Scoped to the Kionga release namespace: a Role there, no ClusterRole, and
  # only Deployments annotated secret.reloader.stakater.com/reload are rolled.
  run helm upgrade --install reloader stakater/reloader "${helm_ctx[@]+"${helm_ctx[@]}"}" --namespace reloader --create-namespace \
    --version "$RELOADER_CHART_VERSION" --set reloader.watchGlobally=false --set "reloader.namespaces={$kionga_namespace}" \
    --set reloader.reloadStrategy=annotations --set reloader.ignoreConfigMaps=true --wait --timeout 10m
fi

if ! skipped openbao; then
  echo "OpenBao is installed and sealed. Initialize and unseal it: scripts/openbao-bootstrap.sh all --key-file PATH (see helm/prerequisites/README.md)."
fi
echo "Prerequisites installed. Next: a DNS-01 ClusterIssuer for *.<applications.baseDomain> (or a wildcard TLS Secret), then the Kionga chart."
