#!/bin/sh
# Public-mode preflight: refuse to start compose.public.yaml with a missing
# setting or with any credential the local stack ships as a default.
#
# Runs in two places with the same rules:
#   - public-up.sh, on the host, after it sources .env;
#   - the public-preflight service in compose.public.yaml, so a direct
#     `docker compose -f compose.yaml -f compose.public.yaml up` fails too.
# POSIX sh on purpose: the container is the busybox shell of the Caddy image.
#
# SECRETS lists NAME=local-default for every credential compose.yaml defaults.
# tests/test_public_preflight.py fails when compose.yaml gains a defaulted
# credential that is missing here, so keep the two in step.
set -eu

REQUIRED="KIONGA_DOMAIN KIONGA_ACME_EMAIL DEX_ADMIN_EMAIL DEX_ADMIN_PASSWORD_HASH TRACE_PROXY_PROJECT_KEY TRACE_PROXY_KEYS SERVING_OBJECTSTORE_ACCESS_KEY SERVING_OBJECTSTORE_SECRET_KEY KIONGA_SANDBOX_TOKEN KIONGA_SANDBOX_IMAGES"

SECRETS="
KIONGA_INTERNAL_TOKEN=kionga-local-service-token
KIONGA_LOCAL_PASSWORD=kionga-local
DEX_CLIENT_SECRET=kionga-local-sso-client-secret
POSTGRES_PASSWORD=kionga-local
OBJECTSTORE_ACCESS_KEY=kionga-local-access
OBJECTSTORE_SECRET_KEY=kionga-local-secret
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_NEXTAUTH_SECRET=kionga-local-nextauth
LANGFUSE_SALT=kionga-local-salt
LANGFUSE_INIT_USER_PASSWORD=kionga-local-admin
JUPYTER_TOKEN=kionga-local
IDE_PASSWORD=kionga-local
KIONGA_WORKSPACE_JOB_TOKEN=kionga-local-workspace-jobs
KIONGA_WORKSPACE_API_TOKEN=kionga-local-workspace-api-identity-change-me
KIONGA_DESIGNER_RUNNER_TOKEN=kionga-local-designer-runner-token
LANGFLOW_SUPERUSER_PASSWORD=kionga-local-langflow-change-me
LANGFLOW_SECRET_KEY=kionga-local-langflow-secret-change-me
"

# Only read when the LDAP connector is on (KIONGA_SSO_LDAP=true).
LDAP_SECRET="DEX_LDAP_BIND_PASSWORD=dex-bind-dev"

# --list prints the table for tests and tooling, then exits.
if [ "${1:-}" = "--list" ]; then
  printf '%s\n' $SECRETS "$LDAP_SECRET"
  exit 0
fi

defaults=""
for pair in $SECRETS $LDAP_SECRET; do
  defaults="$defaults ${pair#*=}"
done

failed=0
fail() {
  echo "public preflight: $1" >&2
  failed=1
}

value_of() {
  eval "printf '%s' \"\${$1:-}\""
}

is_local_default() {
  for known in $defaults; do
    [ "$1" = "$known" ] && return 0
  done
  return 1
}

for name in $REQUIRED; do
  [ -n "$(value_of "$name")" ] || fail "$name is not set (see .env.example)"
done

check_secret() {
  name=$1
  value=$(value_of "$name")
  if [ -z "$value" ]; then
    fail "$name is not set (see .env.example for a generation command)"
  elif is_local_default "$value"; then
    fail "$name uses a built-in local default; generate a new value (see .env.example)"
  fi
}

for pair in $SECRETS; do
  check_secret "${pair%%=*}"
done
if [ "$(value_of KIONGA_SSO_LDAP)" = "true" ]; then
  check_secret "${LDAP_SECRET%%=*}"
fi

# Sandboxes run under gVisor in public mode. compose.public.yaml pins both values
# on the sandbox-manager service; refusing them here means a .env that asks for
# something else stops the start rather than being silently ignored.
token=$(value_of KIONGA_SANDBOX_TOKEN)
if [ -n "$token" ] && [ "${#token}" -lt 32 ]; then
  fail "KIONGA_SANDBOX_TOKEN must be at least 32 characters (see .env.example)"
fi
case "$(value_of KIONGA_SANDBOX_ALLOW_UNSAFE_RUNTIME)" in
  ""|0) ;;
  *) fail "KIONGA_SANDBOX_ALLOW_UNSAFE_RUNTIME is for local development only; remove it from the public configuration" ;;
esac
case "$(value_of KIONGA_SANDBOX_RUNTIME)" in
  ""|runsc) ;;
  *) fail "KIONGA_SANDBOX_RUNTIME must be runsc in public mode" ;;
esac
# Every entry names one image by digest, so a moved tag can never reach a sandbox.
for image in $(value_of KIONGA_SANDBOX_IMAGES | tr ',' ' '); do
  printf '%s' "$image" | grep -Eq '^[^[:space:]@]+@sha256:[a-f0-9]{64}$' ||
    fail "KIONGA_SANDBOX_IMAGES entries must be pinned as repository@sha256:digest"
done

if [ "$failed" -ne 0 ]; then
  echo "public preflight: refusing to start the public stack with missing or default credentials" >&2
  exit 1
fi
echo "public preflight: ok"
