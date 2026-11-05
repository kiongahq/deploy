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

REQUIRED="MLAIOPS_DOMAIN MLAIOPS_ACME_EMAIL DEX_ADMIN_EMAIL DEX_ADMIN_PASSWORD_HASH TRACE_PROXY_PROJECT_KEY TRACE_PROXY_KEYS SERVING_OBJECTSTORE_ACCESS_KEY SERVING_OBJECTSTORE_SECRET_KEY"

SECRETS="
MLAIOPS_INTERNAL_TOKEN=kionga-local-service-token
MLAIOPS_LOCAL_PASSWORD=mlaiops-local
DEX_CLIENT_SECRET=kionga-local-sso-client-secret
POSTGRES_PASSWORD=mlaiops-local
OBJECTSTORE_ACCESS_KEY=mlaiops
OBJECTSTORE_SECRET_KEY=mlaiops-local-secret
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_NEXTAUTH_SECRET=mlaiops-local-nextauth
LANGFUSE_SALT=mlaiops-local-salt
LANGFUSE_INIT_USER_PASSWORD=mlaiops-local-admin
JUPYTER_TOKEN=mlaiops-local
IDE_PASSWORD=mlaiops-local
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

if [ "$failed" -ne 0 ]; then
  echo "public preflight: refusing to start the public stack with missing or default credentials" >&2
  exit 1
fi
echo "public preflight: ok"
