#!/usr/bin/env bash
# Optional: install gVisor's runsc on this Linux host and register it with Docker.
# Nothing else in this repository calls this script. Run it as root:
#   sudo make sandbox-runtime-install
#
# The default release is pinned by SHA-512 for each architecture. To install another
# one, set both KIONGA_RUNSC_RELEASE (such as 20261026.0) and KIONGA_RUNSC_SHA512,
# the value of gvisor.tar.bz2.sha512 for that release and this architecture.
# --dry-run prints what would happen and changes nothing.
set -euo pipefail

PINNED_RELEASE=20261026.0
PINNED_SHA512_x86_64=26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a
PINNED_SHA512_aarch64=da177d9bcfd9ac23899ecfedc6fd47358e9684a1f7e3f5f8ffb8bf7c0ca65f4c08730c0c503b9231b522db28b04c132a06d04d6719e0d8c04c9f82edaf03d8ae

RELEASE="${KIONGA_RUNSC_RELEASE:-$PINNED_RELEASE}"
BIN_DIR="${KIONGA_RUNSC_BIN_DIR:-/usr/local/bin}"
BINARIES=(runsc containerd-shim-runsc-v1)
DRY_RUN=0
[[ "${1:-}" == --dry-run ]] && DRY_RUN=1

fail() { printf 'install-runsc: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || fail "gVisor runs on Linux hosts only. On this machine sandbox tools report sandbox_unavailable; README.md describes the development opt-in."
[[ "$RELEASE" =~ ^[0-9]{8}(\.[0-9]+)?$ ]] || fail "KIONGA_RUNSC_RELEASE must be a release date such as $PINNED_RELEASE"
case "$(uname -m)" in
  x86_64) arch=x86_64 expected="$PINNED_SHA512_x86_64" ;;
  aarch64) arch=aarch64 expected="$PINNED_SHA512_aarch64" ;;
  *) fail "gVisor publishes no build for $(uname -m)" ;;
esac
if [[ "$RELEASE" != "$PINNED_RELEASE" || -n "${KIONGA_RUNSC_SHA512:-}" ]]; then
  expected="${KIONGA_RUNSC_SHA512:-}"
  [[ "$expected" =~ ^[0-9a-f]{128}$ ]] || fail "release $RELEASE needs KIONGA_RUNSC_SHA512, the 128 hex characters of its gvisor.tar.bz2.sha512"
fi
url="https://storage.googleapis.com/gvisor/releases/release/$RELEASE/$arch/gvisor.tar.bz2"

if ((DRY_RUN)); then
  printf 'would download %s\n' "$url"
  printf 'would verify SHA-512 %s\n' "$expected"
  printf 'would install %s and gvisor-bin/ to %s\n' "${BINARIES[*]}" "$BIN_DIR"
  printf 'would run %s/runsc install and reload Docker\n' "$BIN_DIR"
  exit 0
fi

[[ "$(id -u)" == 0 ]] || fail "run as root: sudo make sandbox-runtime-install"
command -v docker >/dev/null || fail "Docker is not installed. For Podman, install runsc yourself and add it under [engine.runtimes] in containers.conf."
command -v bzip2 >/dev/null || fail "bzip2 is required to unpack the release"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --output "$work/gvisor.tar.bz2" "$url"
# A failed check stops the script before anything is unpacked or installed.
(cd "$work" && printf '%s  gvisor.tar.bz2\n' "$expected" | sha512sum --check --quiet -) || fail "checksum mismatch for $url; nothing was installed"
# Only these names leave the archive. runsc executes the sidecar binaries in
# gvisor-bin/ and looks for that directory next to itself, so they move together.
mkdir "$work/unpacked"
tar -xjf "$work/gvisor.tar.bz2" -C "$work/unpacked" --no-same-owner "${BINARIES[@]}" gvisor-bin
install -d -m 0755 "$BIN_DIR/gvisor-bin"
for file in "$work"/unpacked/gvisor-bin/*; do
  install -m 0755 "$file" "$BIN_DIR/gvisor-bin/$(basename "$file")"
done
for file in "${BINARIES[@]}"; do
  install -m 0755 "$work/unpacked/$file" "$BIN_DIR/$file"
done
"$BIN_DIR/runsc" install
systemctl reload docker
printf 'Installed. Verify with: make sandbox-runtime-verify\n'
