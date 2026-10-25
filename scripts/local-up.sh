#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/compose.yaml"
ENGINE=(bash "$ROOT/scripts/container.sh")
MAX_ATTEMPTS="${KIONGA_START_RETRIES:-4}"
START_TIMEOUT_SECONDS="${KIONGA_START_TIMEOUT_SECONDS:-240}"
REBUILD="${KIONGA_REBUILD:-0}"
BUILD_MODE="${KIONGA_BUILD_MODE:-never}"

# Compose discovers .env from the working directory. Running from the repository
# root keeps direct script usage identical to `make local-up`.
cd "$ROOT"

# Cold starts download several large upstream images. Limiting concurrent
# downloads makes Docker Desktop and less reliable links substantially less
# likely to terminate a registry/CDN transfer with EOF.
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-2}"

# Local images do not need SCM attestations; CI/release builds can override both.
export BUILDX_GIT_INFO="${BUILDX_GIT_INFO:-false}"
export BUILDX_GIT_LABELS="${BUILDX_GIT_LABELS:-false}"

# Do not mistake an intentional Ctrl-C/termination for a transient pull failure.
trap 'printf "\nKionga startup cancelled. Re-run make local-up to resume cached layers.\n" >&2; exit 130' INT
trap 'printf "\nKionga startup terminated. Re-run make local-up to resume cached layers.\n" >&2; exit 143' TERM

compose() {
  "${ENGINE[@]}" compose -f "$COMPOSE_FILE" "$@"
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

require_free_space() {
  local purpose="$1" minimum_gb="$2" override="$3" available_kib
  [[ "$minimum_gb" =~ ^[0-9]+$ ]] || fail "$override must be a non-negative integer"
  ((minimum_gb > 0)) || return 0
  command -v df >/dev/null 2>&1 || return 0
  available_kib="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR == 2 {print $4}')"
  [[ "$available_kib" =~ ^[0-9]+$ ]] || return 0
  if ((available_kib < minimum_gb * 1024 * 1024)); then
    fail "$purpose require at least ${minimum_gb} GiB free on the host; about $((available_kib / 1024 / 1024)) GiB remains. Podman/Docker image layers live in a VM or container store backed by this disk. Free space, use 'make local-start' for already cached images, or set $override=0 to override."
  fi
}

# Probe the address Docker will bind (loopback unless KIONGA_BIND_ADDRESS
# widens it). Probing 0.0.0.0 instead misses a native Postgres or Redis on
# 127.0.0.1: on macOS both binds succeed, and Docker then fails to publish.
port_available() {
  python3 -c 'import socket, sys; s = socket.socket(); s.bind((sys.argv[1], int(sys.argv[2])))' \
    "${KIONGA_BIND_ADDRESS:-127.0.0.1}" "$1" >/dev/null 2>&1
}

# Every host port the default profile publishes: service, container port,
# variable, default. tests/test_local_up_ports.py keeps this in step with
# compose.yaml.
PUBLISHED_PORTS=(
  "gateway 8080 GATEWAY_PORT 8080"
  "postgres 5432 POSTGRES_PORT 5432"
  "redis 6379 REDIS_PORT 6379"
  "kafka 9092 KAFKA_PORT 9092"
  "kafka-rest 8082 KAFKA_REST_PORT 8082"
  "objectstore 9000 OBJECTSTORE_PORT 9000"
  "objectstore 9001 OBJECTSTORE_CONSOLE_PORT 9001"
  "mlflow 5000 MLFLOW_PORT 15000"
  "dex 5556 DEX_PORT 5556"
  "feature-gateway 8083 FEATURE_GATEWAY_PORT 8083"
  "storage-proxy 8084 STORAGE_PROXY_PORT 8084"
  "langfuse 3000 LANGFUSE_PORT 3000"
  "prefect-server 4200 PREFECT_PORT 4200"
  "serving-manager 8085 SERVING_MANAGER_PORT 8085"
  "trace-proxy 8081 TRACE_PROXY_PORT 8081"
  "agent-runtime 9000 AGENT_RUNTIME_PORT 19000"
  "jupyter 8888 JUPYTER_PORT 8888"
)

# choose_ports keeps each configured host port when it is free or already held
# by this stack, and otherwise moves it to the first free port of
# <default>+10000..+10004 (5432 -> 15432), exporting the variable so Compose
# and every URL built from it agree. A port set explicitly (environment or
# .env) is never moved: that fails with the variable to change.
choose_ports() {
  local entry service target var default configured existing candidate offset i
  local rendered reserved=" " # space-delimited: bash 3.2 (macOS) treats an empty array as unset under set -u
  local -a configured_ports
  rendered="$(compose config --format json)" || fail 'could not render the Docker Compose configuration'
  # Pass 1: every configured port is reserved, so a moved port never lands on
  # another service's port (9000 + 10000 is agent-runtime's 19000).
  for i in "${!PUBLISHED_PORTS[@]}"; do
    read -r service target var default <<<"${PUBLISHED_PORTS[$i]}"
    configured_ports[i]="$(python3 -c 'import json,sys; s=json.loads(sys.stdin.read())["services"].get(sys.argv[1], {}); print(next((str(p["published"]) for p in s.get("ports", []) if int(p["target"]) == int(sys.argv[2])), ""))' "$service" "$target" <<<"$rendered")"
    reserved+="${configured_ports[i]} "
  done
  # Pass 2: keep free ports and this stack's own; move busy defaults.
  for i in "${!PUBLISHED_PORTS[@]}"; do
    read -r service target var default <<<"${PUBLISHED_PORTS[$i]}"
    configured="${configured_ports[i]}"
    [[ -n "$configured" ]] || continue
    existing="$(compose port "$service" "$target" 2>/dev/null | sed -n '1s/.*://p' || true)"
    if [[ "$existing" == "$configured" ]] || port_available "$configured"; then
      continue
    fi
    if [[ -n "${!var+x}" || "$configured" != "$default" ]]; then
      fail "$service port $configured is in use. Set $var to a free port (in .env or the environment) and run make local-up again."
    fi
    for offset in 10000 10001 10002 10003 10004; do
      candidate=$((default + offset))
      ((candidate <= 65535)) || continue
      [[ "$reserved" == *" $candidate "* ]] && continue
      if [[ "$existing" == "$candidate" ]] || port_available "$candidate"; then
        export "$var=$candidate"
        reserved+="$candidate "
        printf 'Port %s is in use; %s uses %s instead (%s=%s).\n' "$configured" "$service" "$candidate" "$var" "$candidate"
        continue 2
      fi
    done
    fail "$service ports $configured and $((default + 10000))-$((default + 10004)) are occupied; set $var to a free port."
  done
  GATEWAY_PORT="${GATEWAY_PORT:-8080}"
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
  # Registry images only: anything no service builds (a service that reuses
  # another service's local build must not be pulled; see build_inputs.py).
  local image output status=0
  local -a missing=()
  while IFS= read -r image; do
    [[ -n "$image" ]] || continue
    "${ENGINE[@]}" image inspect "$image" >/dev/null 2>&1 || missing+=("$image")
  done < <(compose config --format json | python3 "$ROOT/scripts/build_inputs.py" registry-images)

  if ((${#missing[@]} == 0)); then
    printf 'All upstream images are cached; skipping registry checks.\n'
    return 0
  fi
  require_free_space "Image downloads" "${KIONGA_MIN_PULL_FREE_GB:-5}" KIONGA_MIN_PULL_FREE_GB
  for image in "${missing[@]}"; do
    printf '  pulling: %s\n' "$image"
    if ! output="$("${ENGINE[@]}" pull -q "$image" 2>&1)"; then
      printf '%s\n' "$output" >&2
      if [[ "$output" =~ (pull\ access\ denied|repository\ does\ not\ exist|manifest\ unknown|manifest\ .*not\ found) ]]; then
        return 2
      fi
      status=1
    fi
  done
  return "$status"
}

BUILD_STATE="$ROOT/.local-state/build-inputs.json"

start_platform() {
  local -a args=(up --detach --pull never --remove-orphans)
  local stale=""
  if [[ "$BUILD_MODE" == never ]]; then
    printf 'Starting only cached images; no image downloads or builds.\n'
    compose "${args[@]}" --no-build
    return
  fi
  case "$REBUILD" in
    1|true|TRUE|yes|YES)
      require_free_space "Container builds" "${KIONGA_MIN_BUILD_FREE_GB:-15}" KIONGA_MIN_BUILD_FREE_GB
      check_build_context || return 1
      compose build || return 1
      record_build_inputs || return 1
      ;;
    0|false|FALSE|no|NO)
      # A cached image is reused only while its Dockerfile and COPY inputs are
      # unchanged; otherwise that service alone is rebuilt.
      stale="$(stale_services)" || return 1
      if [[ -n "$stale" ]]; then
        printf 'Rebuilding images whose sources changed: %s\n' "$stale"
        require_free_space "Container builds" "${KIONGA_MIN_BUILD_FREE_GB:-15}" KIONGA_MIN_BUILD_FREE_GB
        check_build_context || return 1
        # shellcheck disable=SC2086
        compose build $stale || return 1
        # shellcheck disable=SC2086
        record_build_inputs $stale || return 1
      fi
      ;;
    *) fail "KIONGA_REBUILD must be 0/false or 1/true (received '$REBUILD')" ;;
  esac
  args+=(--no-build)
  compose "${args[@]}"
}

stale_services() {
  check_build_context
  compose config --format json | python3 "$ROOT/scripts/build_inputs.py" stale --state "$BUILD_STATE"
}

record_build_inputs() {
  compose config --format json | python3 "$ROOT/scripts/build_inputs.py" record --state "$BUILD_STATE" "$@"
}

# Every image builds from a sibling checkout under KIONGA_SRC (see compose.yaml).
export KIONGA_SRC="${KIONGA_SRC:-$(cd "$ROOT/.." && pwd)}"
REQUIRED_REPOS=(platform serving trace-proxy agent-runtime pipeline-runner sdk-python workspace)

check_build_context() {
  local repo missing=()
  for repo in "${REQUIRED_REPOS[@]}"; do
    [[ -d "$KIONGA_SRC/$repo/.git" || -f "$KIONGA_SRC/$repo/.git" ]] || missing+=("$repo")
  done
  [[ ${#missing[@]} -eq 0 ]] || fail "missing component checkouts under $KIONGA_SRC: ${missing[*]}. Run 'make workspace'."
}

diagnostics() {
  printf '\n==> Service status\n' >&2
  compose ps --all >&2 || true
  printf '\n==> Recent bootstrap logs\n' >&2
  compose logs --no-color --tail 40 gateway postgres kafka kafka-rest objectstore mlflow >&2 || true
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
  [[ "$BUILD_MODE" == auto || "$BUILD_MODE" == never ]] || fail "KIONGA_BUILD_MODE must be auto or never"
  "${ENGINE[@]}" compose version >/dev/null 2>&1 || fail "A container engine and Compose provider are required."
  "${ENGINE[@]}" info >/dev/null 2>&1 || fail "The selected container engine is not running. Start Docker or the Podman machine and retry."
  warn_low_disk_space
  choose_ports

  if [[ "$BUILD_MODE" == auto ]]; then
    printf 'Starting Kionga with at most %s concurrent image downloads.\n' "$COMPOSE_PARALLEL_LIMIT"
    printf 'Image downloads and Dockerfile RUN commands execute in the selected container engine; this terminal only shows their output. Image layers still use host-backed disk.\n'
    retry "Download missing upstream images" \
      pull_missing_upstream_images || {
        diagnostics
        fail "image downloads still failed after $MAX_ATTEMPTS attempts. Re-run 'make local-up'; completed layers remain cached."
      }
  fi

  if [[ "$BUILD_MODE" == never ]]; then
    start_platform || {
      diagnostics
      fail "a required image is not cached or the stack could not start. 'make local-up' never downloads or builds; use 'make local-build-up' when you have enough disk space, or deploy to a remote Kubernetes cluster."
    }
  else
    retry "Build missing images and start the platform" start_platform || {
      diagnostics
      fail "container builds or platform startup failed after $MAX_ATTEMPTS attempts."
    }
  fi

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

# Sourcing the script (tests) defines the functions without starting anything.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
