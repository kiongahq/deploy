#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose.yaml"
MAX_ATTEMPTS="${KIONGA_START_RETRIES:-4}"
START_TIMEOUT_SECONDS="${KIONGA_START_TIMEOUT_SECONDS:-240}"
GATEWAY_PORT_EXPLICIT="${GATEWAY_PORT+x}"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
REBUILD="${KIONGA_REBUILD:-0}"

# Compose discovers .env from the working directory. Running from the repository
# root keeps direct script usage identical to `make local-up`.
cd "$ROOT"

# Cold starts download several large upstream images. Limiting concurrent
# downloads makes Docker Desktop and less reliable links substantially less
# likely to terminate a registry/CDN transfer with EOF.
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-2}"

# Buildx provenance discovery shells out to Git. This repository is commonly
# kept under macOS Documents/iCloud, where an evicted or busy .git object can
# make that optional metadata probe hang after every image has already built.
# Local images do not need SCM attestations; CI/release builds can override both.
export BUILDX_GIT_INFO="${BUILDX_GIT_INFO:-false}"
export BUILDX_GIT_LABELS="${BUILDX_GIT_LABELS:-false}"

# Do not mistake an intentional Ctrl-C/termination for a transient pull failure.
trap 'printf "\nKionga startup cancelled. Re-run make local-up to resume cached layers.\n" >&2; exit 130' INT
trap 'printf "\nKionga startup terminated. Re-run make local-up to resume cached layers.\n" >&2; exit 143' TERM

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

fail() {
  printf 'Kionga startup failed: %s\n' "$*" >&2
  exit 1
}

require_positive_integer() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || fail "$name must be a positive integer (received '$value')"
}

warn_low_disk_space() {
  local available_kib minimum_kib=$((25 * 1024 * 1024))
  command -v df >/dev/null 2>&1 || return 0
  available_kib="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR == 2 {print $4}')"
  [[ "$available_kib" =~ ^[0-9]+$ ]] || return 0
  if ((available_kib < minimum_kib)); then
    printf 'Warning: only about %d GiB is free on the host. A completely cold Kionga build recommends 25-35 GiB; cached starts need much less.\n' \
      "$((available_kib / 1024 / 1024))" >&2
  fi
}

port_available() {
  python3 -c 'import socket, sys; s = socket.socket(); s.bind(("0.0.0.0", int(sys.argv[1])))' "$1" \
    >/dev/null 2>&1
}

choose_gateway_port() {
  local candidate configured existing
  configured="$(compose config --format json | python3 -c 'import json,sys; p=json.load(sys.stdin)["services"]["gateway"]["ports"]; print(next(str(x["published"]) for x in p if int(x["target"]) == 8080))')" \
    || fail 'could not resolve the gateway port from Docker Compose'
  if [[ -z "$GATEWAY_PORT_EXPLICIT" && "$configured" != 8080 ]]; then
    GATEWAY_PORT_EXPLICIT=1 # A port set in .env is also an explicit choice.
  fi
  GATEWAY_PORT="$configured"
  existing="$(compose port gateway 8080 2>/dev/null | sed -n '1s/.*://p' || true)"
  if port_available "$GATEWAY_PORT" || [[ "$existing" == "$GATEWAY_PORT" ]]; then
    return 0
  fi
  if [[ -n "$GATEWAY_PORT_EXPLICIT" ]]; then
    fail "gateway port $GATEWAY_PORT is in use. Choose another, e.g. GATEWAY_PORT=18080 make local-up."
  fi
  if [[ "$existing" =~ ^1808[0-4]$ ]]; then
    GATEWAY_PORT="$existing"
    export GATEWAY_PORT
    printf 'Port 8080 is in use; reusing gateway port %s.\n' "$GATEWAY_PORT"
    return 0
  fi
  for candidate in 18080 18081 18082 18083 18084; do
    if port_available "$candidate"; then
      GATEWAY_PORT="$candidate"
      export GATEWAY_PORT
      printf 'Port 8080 is in use; using gateway port %s instead.\n' "$GATEWAY_PORT"
      return 0
    fi
  done
  fail 'gateway ports 8080 and 18080-18084 are occupied; set GATEWAY_PORT to a free port.'
}

retry() {
  local description="$1"
  shift
  local attempt delay status
  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    printf '\n==> %s (attempt %d/%d)\n' "$description" "$attempt" "$MAX_ATTEMPTS"
    if "$@"; then
      return 0
    else
      status=$?
    fi
    if ((status == 2)); then
      printf 'Permanent failure during "%s"; check the image reference or registry access.\n' "$description" >&2
      return 2
    fi
    if ((attempt == MAX_ATTEMPTS)); then
      return 1
    fi
    delay=$((attempt * 3))
    printf 'Transient failure during "%s"; retrying in %ds...\n' \
      "$description" "$delay" >&2
    sleep "$delay"
  done
}

pull_missing_upstream_images() {
  local image missing=0 output
  while IFS= read -r image; do
    [[ -n "$image" ]] || continue
    # Compose-generated images for repository build contexts use this prefix;
    # `compose up` builds them when absent. Only registry images belong here.
    case "$image" in mlaiops-*) continue ;; esac
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      printf '  missing: %s\n' "$image"
      missing=1
    fi
  done < <(compose config --images | sort -u)

  if ((missing == 0)); then
    printf 'All upstream images are cached; skipping registry checks.\n'
    return 0
  fi
  if output="$(compose pull --ignore-buildable --policy missing 2>&1)"; then
    printf '%s\n' "$output"
    return 0
  fi
  printf '%s\n' "$output" >&2
  if [[ "$output" =~ (pull\ access\ denied|repository\ does\ not\ exist|manifest\ unknown|manifest\ .*not\ found) ]]; then
    return 2
  fi
  return 1
}

BUILD_STATE="$ROOT/.local-state/build-inputs.json"

start_platform() {
  local -a args=(up --detach --pull never --remove-orphans)
  local stale=""
  case "$REBUILD" in
    1|true|TRUE|yes|YES)
      check_build_context
      compose build
      record_build_inputs
      ;;
    0|false|FALSE|no|NO)
      # A cached image is reused only while its Dockerfile and COPY inputs are
      # unchanged; otherwise that service alone is rebuilt.
      stale="$(stale_services)"
      if [[ -n "$stale" ]]; then
        printf 'Rebuilding images whose sources changed: %s\n' "$stale"
        check_build_context
        # shellcheck disable=SC2086
        compose build $stale
        # shellcheck disable=SC2086
        record_build_inputs $stale
      fi
      ;;
    *) fail "KIONGA_REBUILD must be 0/false or 1/true (received '$REBUILD')" ;;
  esac
  args+=(--no-build)
  compose "${args[@]}"
}

stale_services() {
  compose config --format json | python3 "$ROOT/scripts/build_inputs.py" stale --state "$BUILD_STATE"
}

record_build_inputs() {
  compose config --format json | python3 "$ROOT/scripts/build_inputs.py" record --state "$BUILD_STATE" "$@"
}

check_build_context() {
  # iCloud can evict tracked files under ~/Documents. Buildx then appears to
  # hang at "load .dockerignore" rather than reporting a useful error.
  [[ "$(uname -s)" == Darwin ]] || return 0
  local file
  for file in "$ROOT/.dockerignore" "$ROOT/Dockerfile"; do
    if [[ -n "$(find "$file" -flags +dataless -print 2>/dev/null)" ]]; then
      fail "$file is an iCloud dataless file. Download it in Finder (or move the repo to a non-iCloud folder) before building."
    fi
  done
}

diagnostics() {
  printf '\n==> Service status\n' >&2
  compose ps --all >&2 || true
  printf '\n==> Recent bootstrap logs\n' >&2
  compose logs --no-color --tail 40 gateway postgres kafka kafka-rest minio mlflow >&2 || true
}

published_port() {
  local service="$1" container_port="$2" fallback="$3" binding
  binding="$(compose port "$service" "$container_port" 2>/dev/null | sed -n '1p' || true)"
  if [[ "$binding" =~ :([0-9]+)$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return
  fi
  printf '%s' "$fallback"
}

wait_for_gateway() {
  local port="$1"
  local health_url="http://127.0.0.1:${port}/api/v1/health"
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))

  printf '\n==> Waiting for the Kionga control plane at %s\n' "$health_url"
  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is not installed; skipping the HTTP readiness probe.\n' >&2
    return 0
  fi

  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error --max-time 3 "$health_url" >/dev/null 2>&1; then
      return 0
    fi
    if compose ps --all --status exited --services gateway 2>/dev/null | grep -qx gateway; then
      return 1
    fi
    sleep 2
  done
  return 1
}

main() {
  local gateway_port jupyter_port
  require_positive_integer KIONGA_START_RETRIES "$MAX_ATTEMPTS"
  require_positive_integer KIONGA_START_TIMEOUT_SECONDS "$START_TIMEOUT_SECONDS"
  command -v docker >/dev/null 2>&1 || fail "Docker is required. Install Docker Desktop or Docker Engine with Compose v2."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (the 'docker compose' command was not found)."
  docker info >/dev/null 2>&1 || fail "Docker is installed but its daemon is not running. Start Docker and run 'make local-up' again."
  warn_low_disk_space
  choose_gateway_port

  printf 'Starting Kionga with at most %s concurrent image downloads.\n' "$COMPOSE_PARALLEL_LIMIT"
  retry "Download missing upstream images" \
    pull_missing_upstream_images || {
      diagnostics
      fail "image downloads still failed after $MAX_ATTEMPTS attempts. Re-run 'make local-up'; Docker keeps completed layers."
    }

  retry "Build missing images and start the platform" start_platform || {
      diagnostics
    fail "the platform could not be built and started after $MAX_ATTEMPTS attempts."
    }

  gateway_port="$(published_port gateway 8080 "$GATEWAY_PORT")"
  if ! wait_for_gateway "$gateway_port"; then
    diagnostics
    fail "the gateway did not become ready within ${START_TIMEOUT_SECONDS}s."
  fi

  retry "Create Kafka topics" bash "$ROOT/scripts/local-topics.sh" || {
    diagnostics
    fail "Kafka started, but its platform topics could not be created."
  }

  printf '\nKionga is ready.\n'
  jupyter_port="$(published_port jupyter 8888 "${JUPYTER_PORT:-8888}")"
  printf '  Console:  http://localhost:%s\n' "$gateway_port"
  printf '  Login:    admin / mlaiops-local\n'
  printf '  Jupyter:  http://localhost:%s/workspace.html?tool=workbench (uses your console login)\n' "$gateway_port"
  printf '  IDE:      make ide-up, then open IDE workspace from the console\n'
  printf '  Status:   make local-status\n'
  printf '  Logs:     make local-logs\n'
  printf '  Stop:     make local-down\n'
}

main "$@"
