#!/usr/bin/env bash
# Does the engine really run containers under gVisor? vm/up.py, public-up.sh and
# `make sandbox-runtime-verify` run this.
#
# Only a real container proves it. An engine's runtime list does not: Podman lists
# runsc even when the binary is not installed. So this starts a throwaway container
# with no network and reads its kernel release, which gVisor's kernel reports itself
# (such as 4.19.0-gvisor).
#
# The default image is a multi-architecture index, pinned by digest: the one digest
# covers amd64 and arm64. Every step has a deadline, so a host without network gets
# a message rather than a hang.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME=runsc
PROBE_IMAGE="${KIONGA_RUNSC_PROBE_IMAGE:-busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec}"
PULL_SECONDS="${KIONGA_RUNSC_PROBE_PULL_SECONDS:-120}"
RUN_SECONDS="${KIONGA_RUNSC_PROBE_RUN_SECONDS:-60}"
HELP="See 'make sandbox-runtime-install' on a Linux host, or https://gvisor.dev/docs/user_guide/install/."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Runs the engine with a deadline and keeps its output in $work/output. Returns 124
# when the deadline passed.
engine_within() {
  local seconds="$1" pid watchdog status=0
  shift
  bash "$ROOT/scripts/container.sh" "$@" >"$work/output" 2>&1 &
  pid=$!
  ( sleep "$seconds"; touch "$work/expired"; kill "$pid" ) >/dev/null 2>&1 &
  watchdog=$!
  wait "$pid" || status=$?
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  [[ ! -e "$work/expired" ]] || return 124
  return "$status"
}

if ! bash "$ROOT/scripts/container.sh" image inspect "$PROBE_IMAGE" >/dev/null 2>&1; then
  status=0
  engine_within "$PULL_SECONDS" pull "$PROBE_IMAGE" || status=$?
  if ((status != 0)); then
    if ((status == 124)); then
      printf 'error: pulling the probe image %s took longer than %s seconds.\n' "$PROBE_IMAGE" "$PULL_SECONDS" >&2
    else
      printf 'error: could not pull the probe image %s.\n%s\n' "$PROBE_IMAGE" "$(cat "$work/output")" >&2
    fi
    printf 'This host may have no network or no route to the registry. Load the image from another machine with docker save and docker load, then try again.\n' >&2
    exit 1
  fi
fi

# Podman takes --runtime before the subcommand, and Docker after it.
engine="${KIONGA_ENGINE:-}"
[[ -n "$engine" || ! -f "$ROOT/.local-state/container-engine" ]] || engine="$(cat "$ROOT/.local-state/container-engine")"
if [[ "$engine" == podman ]]; then
  probe=(--runtime "$RUNTIME" run --rm --pull never --network none)
else
  probe=(run --rm --pull never --network none --runtime "$RUNTIME")
fi
status=0
engine_within "$RUN_SECONDS" "${probe[@]}" "$PROBE_IMAGE" uname -r || status=$?
if ((status == 124)); then
  printf 'error: a container under %s did not finish within %s seconds.\n' "$RUNTIME" "$RUN_SECONDS" >&2
  exit 1
fi
if ((status != 0)) && grep -q 'unknown flag: --runtime' "$work/output"; then
  # The Podman client on macOS and Windows talks to a VM and has no runtime flag.
  printf 'error: this Podman client cannot choose a runtime, so it cannot run the probe. Run this check on the Linux host.\n' >&2
  exit 1
fi
if ((status != 0)); then
  printf 'error: a container failed to start under %s. %s\n%s\n' "$RUNTIME" "$HELP" "$(cat "$work/output")" >&2
  exit 1
fi
if ! grep -qi gvisor "$work/output"; then
  printf 'error: the container started, but its kernel release does not mention gVisor. %s\n' "$HELP" >&2
  exit 1
fi
printf 'A container ran under gVisor.\n'
