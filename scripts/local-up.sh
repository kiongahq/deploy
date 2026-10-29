#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/compose.yaml"
ENGINE=(bash "$ROOT/scripts/container.sh")
MAX_ATTEMPTS="${KIONGA_START_RETRIES:-4}"
START_TIMEOUT_SECONDS="${KIONGA_START_TIMEOUT_SECONDS:-240}"
REBUILD="${KIONGA_REBUILD:-0}"
# missing (make local-up): pull and build only what is not cached, never
# rebuild stale images. auto (make local-build-up): also rebuild images whose
# sources changed. never (make local-start): cached images only.
BUILD_MODE="${KIONGA_BUILD_MODE:-missing}"
PORT_STATE="$ROOT/.local-state/ports.env"
# Port variables the operator set in the shell for this command. Only these are
# never moved; ports from .env, defaults and remembered choices may move.
PINNED_PORTS=" "
for _var in $(env | sed -n 's/^\([A-Z_]*_PORT\)=.*/\1/p'); do PINNED_PORTS+="$_var "; done
unset _var

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
    if [[ "$PINNED_PORTS" == *" $var "* ]]; then
      fail "$service port $configured is in use and $var=$configured was set for this command. Choose a free port, or unset $var to let make local-up pick one."
    fi
    for candidate in $((default + 10000)) $((default + 10001)) $((default + 10002)) $((default + 10003)) $((default + 10004)) \
        $((configured + 1)) $((configured + 2)) $((configured + 3)) $((configured + 4)) $((configured + 5)); do
      ((candidate <= 65535)) || continue
      [[ "$candidate" == "$configured" ]] && continue
      [[ "$reserved" == *" $candidate "* ]] && continue
      if [[ "$existing" == "$candidate" ]] || port_available "$candidate"; then
        export "$var=$candidate"
        reserved+="$candidate "
        remember_port "$var" "$candidate"
        printf 'Port %s is in use; %s uses %s instead (%s=%s, remembered in .local-state/ports.env).\n' "$configured" "$service" "$candidate" "$var" "$candidate"
        continue 2
      fi
    done
    fail "$service port $configured and every fallback are occupied; set $var to a free port."
  done
  GATEWAY_PORT="${GATEWAY_PORT:-8080}"
}

# Remembered port moves keep URLs stable across restarts. A variable set in
# the shell for this command wins over the remembered value.
load_port_choices() {
  local line var value
  [[ -f "$PORT_STATE" ]] || return 0
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z_]+_PORT)=([0-9]+)$ ]] || continue
    var="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    [[ "$PINNED_PORTS" == *" $var "* ]] && continue
    export "$var=$value"
  done <"$PORT_STATE"
}

remember_port() {
  local var="$1" value="$2" kept=""
  mkdir -p "$(dirname "$PORT_STATE")"
  [[ -f "$PORT_STATE" ]] && kept="$(grep -v "^$var=" "$PORT_STATE" || true)"
  { [[ -n "$kept" ]] && printf '%s\n' "$kept"; printf '%s=%s\n' "$var" "$value"; } >"$PORT_STATE"
}

# The engine container.sh selects (Docker unless .local-state or KIONGA_ENGINE says Podman).
selected_engine() {
  local engine="${KIONGA_ENGINE:-}"
  [[ -z "$engine" && -f "$ROOT/.local-state/container-engine" ]] && engine="$(cat "$ROOT/.local-state/container-engine")"
  printf '%s' "${engine:-docker}"
}

engine_reachable() {
  "${ENGINE[@]}" info >/dev/null 2>&1
}

# A Podman machine can keep running while its port forwarder (gvproxy) is
# dead, e.g. after `kill -9` on a published port: the VM is up, but neither
# the API nor any published port answers. A stop/start brings the forwarder
# back; containers and volumes live inside the VM and survive it. A stale
# socket can make the first start fail, so it is retried once.
recover_engine() {
  local engine attempt
  [[ "${KIONGA_AUTO_RECOVER_ENGINE:-1}" == 0 ]] && return 1
  engine="$(selected_engine)"
  case "$engine" in
    podman)
      command -v podman >/dev/null 2>&1 || return 1
      printf 'The Podman machine is not answering; restarting it (containers and volumes are kept)...\n' >&2
      for attempt in 1 2; do
        podman machine stop >/dev/null 2>&1 || true
        if podman machine start >/dev/null 2>&1 && engine_reachable; then
          printf 'Podman machine restarted.\n' >&2
          return 0
        fi
        sleep 2
      done
      ;;
    docker)
      [[ "$(uname -s)" == Darwin ]] && command -v open >/dev/null 2>&1 || return 1
      printf 'Docker is not answering; starting Docker Desktop...\n' >&2
      open -a Docker >/dev/null 2>&1 || return 1
      for attempt in $(seq 1 60); do
        engine_reachable && { printf 'Docker Desktop is ready.\n' >&2; return 0; }
        sleep 2
      done
      ;;
  esac
  return 1
}

ensure_engine() {
  engine_reachable && "${ENGINE[@]}" compose version >/dev/null 2>&1 && return 0
  if ! engine_reachable; then
    recover_engine || fail "the $(selected_engine) engine is not reachable and could not be restarted. Start Docker or run 'podman machine stop && podman machine start', then retry. (Set KIONGA_AUTO_RECOVER_ENGINE=0 to skip the automatic restart.)"
  fi
  "${ENGINE[@]}" compose version >/dev/null 2>&1 || fail "A Compose provider is required (Docker Compose v2, or podman compose with docker-compose installed)."
}

host_free_kib() {
  df -Pk "$ROOT" 2>/dev/null | awk 'NR == 2 {print $4}'
}

# Space a Podman machine already holds on the host but does not use: its disk
# image is sparse, so blocks the VM allocated once and later freed (deleted
# images) can be reused without growing the file. Builds can use this space
# on top of the host's free space. 0 when unknown or not Podman.
engine_slack_kib() {
  local name image allocated used
  [[ "$(selected_engine)" == podman ]] || { printf 0; return; }
  # podman machine inspect does not report the disk image; it lives under the
  # machine's name in Podman's data directory (applehv, libkrun, qemu...).
  name="$(podman machine inspect --format '{{.Name}}' 2>/dev/null | head -n 1 || true)"
  image="$(ls "$HOME"/.local/share/containers/podman/machine/*/"${name:-podman-machine-default}"*.raw 2>/dev/null | head -n 1 || true)"
  [[ -n "$image" && -f "$image" ]] || { printf 0; return; }
  allocated="$(du -k "$image" 2>/dev/null | awk '{print $1}')"
  used="$(podman machine ssh -- df -Pk / 2>/dev/null | awk 'NR == 2 {print $3}')"
  if [[ "$allocated" =~ ^[0-9]+$ && "$used" =~ ^[0-9]+$ ]] && ((allocated > used)); then
    printf '%s' $((allocated - used))
  else
    printf 0
  fi
}

build_space_kib() {
  local free slack
  free="$(host_free_kib)"
  [[ "$free" =~ ^[0-9]+$ ]] || free=0
  slack="$(engine_slack_kib)"
  printf '%s' $((free + slack))
}

# Builds need room. Count the host's free space plus space the engine already
# holds; if that is short, remove dangling (untagged, unused) images, which
# never deletes a tagged image, a container or a volume, and check again.
ensure_build_space() {
  local minimum_gb="${KIONGA_MIN_BUILD_FREE_GB:-15}" available
  [[ "$minimum_gb" =~ ^[0-9]+$ ]] || fail "KIONGA_MIN_BUILD_FREE_GB must be a non-negative integer"
  ((minimum_gb > 0)) || return 0
  available="$(build_space_kib)"
  ((available >= minimum_gb * 1024 * 1024)) && return 0
  if [[ "${KIONGA_AUTO_PRUNE:-1}" != 0 ]]; then
    printf 'About %d GiB is available for builds (%d GiB needed); removing dangling images to make room...\n' \
      "$((available / 1024 / 1024))" "$minimum_gb" >&2
    "${ENGINE[@]}" image prune -f >/dev/null 2>&1 || true
    available="$(build_space_kib)"
    printf 'About %d GiB is now available for builds.\n' "$((available / 1024 / 1024))" >&2
    ((available >= minimum_gb * 1024 * 1024)) && return 0
  fi
  fail "Container builds need about ${minimum_gb} GiB; about $((available / 1024 / 1024)) GiB is available (host free space plus space the container engine already holds). Free disk space, or set KIONGA_MIN_BUILD_FREE_GB to a lower value if you know the build fits."
}

missing_services() {
  compose config --format json | python3 "$ROOT/scripts/build_inputs.py" missing
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
  local image output status=0 status_ok
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
  printf '%d upstream image(s) are not cached. A first start downloads about 1.5 GB of images and then builds Kionga'"'"'s own; on a slow link this takes a while. Ctrl-C is safe: finished images stay cached and the next run continues.\n' "${#missing[@]}"
  local index=0 started
  for image in "${missing[@]}"; do
    index=$((index + 1))
    started=$SECONDS
    printf '\n  [%d/%d] pulling %s\n' "$index" "${#missing[@]}" "$image"
    # On a terminal the engine draws its own progress bars (it only does so
    # when attached to the terminal). A failed pull is retried quietly once
    # to capture the message that tells a permanent error from a transient one.
    output=""
    if [[ -t 1 ]]; then
      "${ENGINE[@]}" pull "$image" && status_ok=1 || status_ok=0
      ((status_ok)) || { output="$("${ENGINE[@]}" pull -q "$image" 2>&1)" && status_ok=1 || status_ok=0; }
    else
      output="$("${ENGINE[@]}" pull -q "$image" 2>&1)" && status_ok=1 || status_ok=0
    fi
    if ((status_ok)); then
      printf '  [%d/%d] done in %ds\n' "$index" "${#missing[@]}" $((SECONDS - started))
    else
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
  local stale="" absent=""
  if [[ "$BUILD_MODE" == never ]]; then
    printf 'Starting only cached images; no image downloads or builds.\n'
    compose "${args[@]}" --no-build
    return
  fi
  if [[ "$BUILD_MODE" == missing ]]; then
    # Build only images that do not exist; stale ones keep running as cached
    # (make local-build-up rebuilds them).
    absent="$(missing_services)" || return 1
    if [[ -n "$absent" ]]; then
      printf 'Building images that are not cached yet: %s\n' "$absent"
      ensure_build_space
      check_build_context || return 1
      # shellcheck disable=SC2086
      compose build $absent || return 1
      # shellcheck disable=SC2086
      record_build_inputs $absent || return 1
    fi
    compose "${args[@]}" --no-build
    return
  fi
  case "$REBUILD" in
    1|true|TRUE|yes|YES)
      ensure_build_space
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
        ensure_build_space
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
  [[ "$BUILD_MODE" == auto || "$BUILD_MODE" == missing || "$BUILD_MODE" == never ]] || fail "KIONGA_BUILD_MODE must be missing, auto or never"
  ensure_engine
  load_port_choices
  choose_ports

  if [[ "$BUILD_MODE" != never ]]; then
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
      fail "a required image is not cached or the stack could not start. 'make local-start' never downloads or builds; use 'make local-up' to fetch and build what is missing."
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
