#!/usr/bin/env bash
# Select the local engine without changing other projects' Docker contexts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
engine="${KIONGA_ENGINE:-}"
if [[ -z "$engine" && -f "$ROOT/.local-state/container-engine" ]]; then
  engine="$(cat "$ROOT/.local-state/container-engine")"
fi
engine="${engine:-docker}"
# Local generated secrets from make local-up, so every compose call (make
# langflow-up recreates the gateway) renders the same values. A variable set in
# the shell wins. public-up.sh calls docker compose directly and never reads this.
if [[ "${1:-}" == compose && -f "$ROOT/.local-state/secrets.env" ]]; then
  while IFS= read -r line; do
    # TRACE_PROXY_KEYS is single-quoted JSON, so the shell and Compose both read it.
    [[ "$line" =~ ^(JUPYTER_TOKEN|IDE_PASSWORD|KIONGA_WORKSPACE_JOB_TOKEN|TRACE_PROXY_PROJECT_KEY)=([A-Za-z0-9._~-]+)$ ]] ||
      [[ "$line" =~ ^(TRACE_PROXY_KEYS)=\'([^\']+)\'$ ]] || continue
    [[ -n "${!BASH_REMATCH[1]:-}" ]] || export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
  done <"$ROOT/.local-state/secrets.env"
fi
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
