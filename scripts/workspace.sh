#!/usr/bin/env bash
# Clone (or fast-forward) every Kionga component beside this repo, the layout
# compose.yaml builds from:  <KIONGA_SRC>/{deploy,platform,serving,...}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${KIONGA_SRC:-$(cd "$ROOT/.." && pwd)}"
BASE="${KIONGA_GIT_BASE:-git@github.com:kiongahq/}"
REPOS=(platform serving trace-proxy agent-runtime pipeline-runner sdk-python workspace contracts docs)
PULL=0
[[ "${1:-}" == "--pull" ]] && PULL=1

failed=0
for repo in "${REPOS[@]}"; do
  dest="$SRC/$repo"
  if [[ -e "$dest/.git" ]]; then
    if [[ "$PULL" == 1 ]]; then
      if [[ -n "$(git -C "$dest" status --porcelain)" ]]; then
        echo "  dirty, not updated: $repo"
      elif git -C "$dest" pull -q --ff-only; then
        echo "  updated: $repo"
      else
        echo "  pull failed: $repo" >&2; failed=1
      fi
    else
      echo "  present: $repo"
    fi
  elif [[ -e "$dest" ]]; then
    echo "  $dest exists but is not a git checkout" >&2; failed=1
  elif git clone -q --recurse-submodules "${BASE}${repo}.git" "$dest"; then
    echo "  cloned: $repo"
  else
    echo "  clone failed: $repo" >&2; failed=1
  fi
done
# The platform pins contracts as a submodule; make sure it is populated.
[[ -e "$SRC/platform/.git" ]] && git -C "$SRC/platform" submodule update --init --quiet || true
echo "Workspace: $SRC"
exit "$failed"
