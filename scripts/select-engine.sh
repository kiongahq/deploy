#!/usr/bin/env bash
# Save the local engine choice for this checkout without changing other projects.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-}" in
  docker|podman) ;;
  *) printf 'Usage: bash scripts/select-engine.sh docker|podman\n' >&2; exit 2 ;;
esac
mkdir -p "$ROOT/.local-state"
printf '%s\n' "$1" > "$ROOT/.local-state/container-engine"
