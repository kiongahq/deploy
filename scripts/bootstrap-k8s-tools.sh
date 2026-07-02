#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${MLOPS_TOOLS_BIN:-$ROOT/.tools.nosync/bin}"
KIND_VERSION="${KIND_VERSION:-v0.31.0}"
HELM_VERSION="${HELM_VERSION:-v3.21.0}"

mkdir -p "$BIN_DIR"

if [[ ! -x "$BIN_DIR/kind" ]]; then
  command -v go >/dev/null || {
    echo "go is required to install kind ${KIND_VERSION}" >&2
    exit 1
  }
  echo "Installing kind ${KIND_VERSION} in ${BIN_DIR}"
  GOBIN="$BIN_DIR" go install "sigs.k8s.io/kind@${KIND_VERSION}"
fi

if [[ ! -x "$BIN_DIR/helm" ]]; then
  command -v curl >/dev/null || { echo "curl is required to install helm" >&2; exit 1; }
  command -v tar >/dev/null || { echo "tar is required to install helm" >&2; exit 1; }

  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in
    x86_64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac

  archive="helm-${HELM_VERSION}-${os}-${arch}.tar.gz"
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  echo "Installing helm ${HELM_VERSION} in ${BIN_DIR}"
  curl --fail --location --silent --show-error \
    "https://get.helm.sh/${archive}" \
    --output "$temp_dir/$archive"
  tar -xzf "$temp_dir/$archive" -C "$temp_dir"
  install -m 0755 "$temp_dir/${os}-${arch}/helm" "$BIN_DIR/helm"
fi

echo "Kubernetes tools are ready in ${BIN_DIR}"
