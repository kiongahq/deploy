#!/usr/bin/env bash
# Concurrency pressure test for the Nexus control plane.
#
#   make local-up && ./scripts/stress-test.sh
#   CONCURRENCY=64 REQUESTS=400 ./scripts/stress-test.sh
#
# Fires many concurrent requests at the transactional write paths and the hot
# read paths, then asserts:
#   - zero 5xx responses (no crashes or transaction failures under load)
#   - every write returned 201
#   - every concurrently-created resource is actually persisted (no lost writes)
#
# Portable: needs only bash, curl, and python3 — no k6. Complements the k6
# load test (tests/load/gateway.js), which measures latency SLOs.
set -uo pipefail

export GATEWAY="${GATEWAY:-http://localhost:8080}"
export TOKEN="${MLAIOPS_TOKEN:-}"
CONCURRENCY="${CONCURRENCY:-32}"
REQUESTS="${REQUESTS:-200}"
export TAG="stress-$(date +%s)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()  { printf '%s\n' "$*"; }
pass=0; fail=0
ok()   { pass=$((pass+1)); say "  ✓ $*"; }
bad()  { fail=$((fail+1)); say "  ✗ $*"; }
count() { grep -cE "$1" "$2" 2>/dev/null | tr -d '[:space:]'; }

# One concurrent write; prints the HTTP status code on stdout.
write_one() {
  if [ -n "${TOKEN}" ]; then
    curl -s -o /dev/null -w '%{http_code}\n' -X POST "$GATEWAY/api/v1/projects" \
      -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
      -d "{\"name\":\"${TAG}-$1\",\"template\":\"tabular-classification\"}"
  else
    curl -s -o /dev/null -w '%{http_code}\n' -X POST "$GATEWAY/api/v1/projects" \
      -H 'Content-Type: application/json' \
      -d "{\"name\":\"${TAG}-$1\",\"template\":\"tabular-classification\"}"
  fi
}

# One concurrent read across a rotation of hot GET endpoints.
read_one() {
  local paths=(/api/v1/dashboard /api/v1/projects /api/v1/models /api/v1/agents \
    /api/v1/features /api/v1/catalog /api/v1/components /api/v1/me)
  local p="${paths[$(( $1 % ${#paths[@]} ))]}"
  if [ -n "${TOKEN}" ]; then
    curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" "$GATEWAY$p"
  else
    curl -s -o /dev/null -w '%{http_code}\n' "$GATEWAY$p"
  fi
}
export -f write_one read_one

say "== concurrency pressure: $REQUESTS requests, $CONCURRENCY-wide =="
say "target: $GATEWAY"

# --- 1. concurrent writes ---------------------------------------------------
seq 1 "$REQUESTS" | xargs -P "$CONCURRENCY" -n1 bash -c 'write_one "$1"' _ > "$TMP/write_codes"
created=$(count '^201$' "$TMP/write_codes"); created=${created:-0}
werr=$(count '^5[0-9][0-9]$' "$TMP/write_codes"); werr=${werr:-0}
[ "$created" -eq "$REQUESTS" ] && ok "all $REQUESTS concurrent writes returned 201" \
  || bad "only $created/$REQUESTS writes returned 201"
[ "$werr" -eq 0 ] && ok "zero 5xx under write pressure" \
  || bad "$werr server errors under write pressure"

# --- 2. no lost writes ------------------------------------------------------
auth_arg=""
[ -n "$TOKEN" ] && auth_arg="-H Authorization: Bearer $TOKEN"
persisted=$(curl -s ${auth_arg:+-H "Authorization: Bearer $TOKEN"} "$GATEWAY/api/v1/projects" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(1 for p in d if str(p.get('name','')).startswith('$TAG')))" 2>/dev/null)
persisted=${persisted:-0}
[ "$persisted" -eq "$created" ] && ok "all $created writes persisted (no lost writes)" \
  || bad "persisted $persisted of $created created (lost writes / read-after-write gap)"

# --- 3. concurrent reads ----------------------------------------------------
seq 1 "$REQUESTS" | xargs -P "$CONCURRENCY" -n1 bash -c 'read_one "$1"' _ > "$TMP/read_codes"
rok=$(count '^200$' "$TMP/read_codes"); rok=${rok:-0}
rerr=$(count '^5[0-9][0-9]$' "$TMP/read_codes"); rerr=${rerr:-0}
[ "$rok" -eq "$REQUESTS" ] && ok "all $REQUESTS concurrent reads returned 200" \
  || bad "only $rok/$REQUESTS reads returned 200"
[ "$rerr" -eq 0 ] && ok "zero 5xx under read pressure" \
  || bad "$rerr server errors under read pressure"

say ""
say "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
