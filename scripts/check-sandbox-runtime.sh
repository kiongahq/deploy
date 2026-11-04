#!/usr/bin/env bash
# Does the engine really run containers under gVisor? public-up.sh and
# `make sandbox-runtime-verify` run this.
#
# Only a real container proves it. An engine's runtime list does not: Podman lists
# runsc even when the binary is not installed. So this starts a throwaway container
# with no network and reads its kernel log, which gVisor's kernel writes itself.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME=runsc
PROBE_IMAGE="${KIONGA_RUNSC_PROBE_IMAGE:-busybox:1.37}"
HELP="See 'make sandbox-runtime-install' on a Linux host, or https://gvisor.dev/docs/user_guide/install/."

if ! banner="$(bash "$ROOT/scripts/container.sh" run --rm --network none --runtime "$RUNTIME" "$PROBE_IMAGE" dmesg 2>&1)"; then
  printf 'error: a container failed to start under %s. %s\n%s\n' "$RUNTIME" "$HELP" "$banner" >&2
  exit 1
fi
if ! grep -qi gvisor <<<"$banner"; then
  printf 'error: the container started, but its kernel log does not mention gVisor. %s\n' "$HELP" >&2
  exit 1
fi
printf 'A container ran under gVisor.\n'
