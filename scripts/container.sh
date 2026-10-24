#!/usr/bin/env bash
# Select the local engine without changing other projects' Docker contexts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
engine="${KIONGA_ENGINE:-}"
if [[ -z "$engine" && -f "$ROOT/.local-state/container-engine" ]]; then
  engine="$(cat "$ROOT/.local-state/container-engine")"
fi
engine="${engine:-docker}"
case "$engine" in
  docker) exec docker "$@" ;;
  podman)
    if [[ "${1:-}" == compose ]]; then
      shift
      export PODMAN_COMPOSE_PROVIDER="${PODMAN_COMPOSE_PROVIDER:-docker-compose}"
      export KIONGA_ENGINE_SOCKET="${KIONGA_ENGINE_SOCKET:-/run/podman/podman.sock}"
      if [[ "${1:-}" == -f && -f "${2:-}" && "$(realpath "$2")" == "$ROOT/compose.yaml" ]]; then
        base="$2"
        shift 2
        if [[ "${1:-}" == build ]]; then
          shift
          exec python3 "$ROOT/scripts/podman-build.py" "$base" "$ROOT/compose.podman.yaml" "$@"
        fi
        exec podman compose -f "$base" -f "$ROOT/compose.podman.yaml" "$@"
      fi
      exec podman compose "$@"
    fi
    exec podman "$@"
    ;;
  *) printf 'Unsupported KIONGA_ENGINE: %s\n' "$engine" >&2; exit 1 ;;
esac
