#!/usr/bin/env bash
# Bootstrap OpenBao for Kionga after helm/prerequisites/install.sh. Every step
# reads the current state first, so running it again changes nothing.
#
# Usage: scripts/openbao-bootstrap.sh COMMAND [options]
#   all         init (with --key-file), unseal, configure; then ca with --ca-out
#   init        initialize once with Shamir shares and unseal every server
#   unseal      unseal every sealed server with the shares in --key-file
#   configure   KV v2 at kionga/, transit key kionga-credential, Kubernetes auth,
#               one policy and role per consumer, optional database engine
#   seed        write platform secrets into KV from files or random values
#   ca          write the OpenBao CA certificate (public) to --ca-out
#   reload-tls  make every server reread its certificate (no sealing)
#   status      print each server's initialized and sealed state
#
# Common options:
#   --kube-context CTX         kubectl context
#   --namespace NS             OpenBao namespace (default openbao)
#   --release NAME             Kionga Helm release (default kionga)
#   --release-namespace NS     Kionga release namespace (default kionga-system)
#   --workspace-namespace NS   Kionga workload namespace (default kionga-workloads)
#   --key-file PATH            init writes the unseal key shares and root token
#                              here once (mode 600) and refuses to overwrite it;
#                              keep it off the cluster. unseal reads it.
#   --key-shares N --key-threshold N   Shamir split (default 5 and 3)
#   --addr URL                 an OpenBao you run yourself: configure and seed
#                              with a local bao CLI; init and unseal stay yours
#   --kubernetes-host URL      API server for Kubernetes auth
#                              (default https://kubernetes.default.svc:443)
#   --kubernetes-ca-file F --token-reviewer-jwt-file F   with --addr only
#   --audience AUD             audience External Secrets tokens carry (default openbao)
#   --gateway-audience AUD     audience the gateway role requires (default none)
#   --with-database            dynamic PostgreSQL credentials for the gateway:
#     --database-connection-url URL   postgresql://{{username}}:{{password}}@host:5432/db?sslmode=verify-full
#     --database-admin-user NAME --database-admin-password-file F
#     --database-group-role ROLE      role the generated users join (default kionga)
#   --ca-out FILE              where ca writes the CA certificate
# seed options:
#   --from-dir DIR             DIR/<path>/<KEY> files become kionga/<path> KEY
#   --generate PATH/KEY=hex:N|base64:N   random value when the key is absent
#   --rotate PATH/KEY=hex:N|base64:N     always write a new random value
#   --overwrite                --from-dir replaces keys that exist
# The token comes from BAO_TOKEN, or else the root token in --key-file.
# Secret values never reach stdout, stderr, command arguments or the cluster
# outside OpenBao: they travel on stdin.
set -euo pipefail

me="openbao-bootstrap"
die() { echo "$me: $*" >&2; exit 1; }
log() { echo "$me: $*" >&2; }
usage() { sed -n '2,46p' "$0"; }

command="${1:-}"
[[ -n "$command" ]] || { usage; exit 2; }
shift
case "$command" in
  all|init|unseal|configure|seed|ca|reload-tls|status) ;;
  -h|--help|help) usage; exit 0 ;;
  *) echo "$me: unknown command $command" >&2; usage >&2; exit 2 ;;
esac

context="" namespace="openbao" release="kionga" release_ns="kionga-system" workspace_ns="kionga-workloads"
key_file="" key_shares=5 key_threshold=3 addr="" kube_host="https://kubernetes.default.svc:443"
kube_ca_file="" reviewer_jwt_file="" audience="openbao" gateway_audience="" ca_out=""
with_database=0 db_url="" db_admin_user="" db_admin_password_file="" db_group_role="kionga"
from_dir="" overwrite=0 generate=() rotate=()
kv_mount="kionga" transit_mount="transit" transit_key="kionga-credential" auth_mount="kubernetes"
db_mount="database" db_config="kionga-postgres"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kube-context) context="$2"; shift 2 ;;
    --namespace) namespace="$2"; shift 2 ;;
    --release) release="$2"; shift 2 ;;
    --release-namespace) release_ns="$2"; shift 2 ;;
    --workspace-namespace) workspace_ns="$2"; shift 2 ;;
    --key-file) key_file="$2"; shift 2 ;;
    --key-shares) key_shares="$2"; shift 2 ;;
    --key-threshold) key_threshold="$2"; shift 2 ;;
    --addr) addr="$2"; shift 2 ;;
    --kubernetes-host) kube_host="$2"; shift 2 ;;
    --kubernetes-ca-file) kube_ca_file="$2"; shift 2 ;;
    --token-reviewer-jwt-file) reviewer_jwt_file="$2"; shift 2 ;;
    --audience) audience="$2"; shift 2 ;;
    --gateway-audience) gateway_audience="$2"; shift 2 ;;
    --with-database) with_database=1; shift ;;
    --database-connection-url) db_url="$2"; shift 2 ;;
    --database-admin-user) db_admin_user="$2"; shift 2 ;;
    --database-admin-password-file) db_admin_password_file="$2"; shift 2 ;;
    --database-group-role) db_group_role="$2"; shift 2 ;;
    --ca-out) ca_out="$2"; shift 2 ;;
    --from-dir) from_dir="$2"; shift 2 ;;
    --generate) generate+=("$2"); shift 2 ;;
    --rotate) rotate+=("$2"); shift 2 ;;
    --overwrite) overwrite=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$me: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Names end up in policy text and command arguments: accept plain names only.
dns='^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
for pair in "namespace:$namespace" "release:$release" "release-namespace:$release_ns" "workspace-namespace:$workspace_ns" \
            "database-group-role:$db_group_role"; do
  [[ "${pair#*:}" =~ $dns ]] || die "--${pair%%:*} must be a lowercase DNS label"
done
[[ -z "$audience" || "$audience" =~ ^[A-Za-z0-9._:/-]+$ ]] || die "--audience has unsupported characters"
[[ -z "$gateway_audience" || "$gateway_audience" =~ ^[A-Za-z0-9._:/-]+$ ]] || die "--gateway-audience has unsupported characters"
[[ "$key_shares" =~ ^[0-9]+$ && "$key_threshold" =~ ^[0-9]+$ ]] || die "--key-shares and --key-threshold take numbers"
(( key_threshold >= 1 && key_threshold <= key_shares && key_shares <= 10 )) || die "need 1 <= threshold <= shares <= 10"
[[ "$kube_host" =~ ^https://[^[:space:]]+$ ]] || die "--kubernetes-host must be an https URL"
[[ -z "$addr" || "$addr" =~ ^https://[^[:space:]]+$ ]] || die "--addr must be an https URL"
if [[ -z "$addr" && ( -n "$kube_ca_file" || -n "$reviewer_jwt_file" ) ]]; then
  die "--kubernetes-ca-file and --token-reviewer-jwt-file are for an OpenBao outside the cluster (--addr)"
fi

command -v python3 >/dev/null || die "python3 is required"
if [[ -n "$addr" ]]; then
  command -v bao >/dev/null || die "bao (the OpenBao CLI) is required with --addr"
else
  command -v kubectl >/dev/null || die "kubectl is required"
fi
kube=(kubectl)
[[ -n "$context" ]] && kube+=(--context "$context")

# Consumers. Each gets one policy and one Kubernetes auth role.
platform_sa="$release-openbao-reader"
gateway_sa="$release-gateway"
platform_paths=(shared gateway operator worker features storage tracing agent-designer sandbox secret-references)
workspace_paths=(model-storage "workloads/*")

# JSON helpers: values are parsed here and never printed unless asked for by key.
# The program is passed with -c so stdin stays free for the JSON it reads.
py_helper="$(cat <<'PY'
import json, os, stat, sys

cmd, args = sys.argv[1], sys.argv[2:]

def load():
    text = sys.stdin.read()
    return json.loads(text) if text.strip() else {}

def walk(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            sys.exit(3)
        data = data[part]
    return data

if cmd == "get":
    value = walk(load(), args)
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, list):
        print("\n".join(str(v) for v in value))
    elif value is None:
        print("")
    else:
        print(value)
elif cmd == "keys":
    value = walk(load(), args)
    print("\n".join(sorted(value)) if isinstance(value, dict) else "")
elif cmd == "init-ok":
    data = json.load(open(args[0]))
    keys = data.get("unseal_keys_b64") or []
    sys.exit(0 if len(keys) == int(args[1]) and data.get("root_token") else 1)
elif cmd == "unseal-key":
    sys.stdout.write(json.load(open(args[0]))["unseal_keys_b64"][int(args[1])])
elif cmd == "root-token":
    sys.stdout.write(json.load(open(args[0]))["root_token"])
elif cmd == "private":
    mode = os.stat(args[0]).st_mode
    sys.exit(0 if not mode & (stat.S_IRWXG | stat.S_IRWXO) else 1)
elif cmd == "same":
    # same FIELD=JSON...: stdin is a read response; compare data fields.
    # A dotted FIELD reaches into a nested object.
    data = (load().get("data") or {})
    for item in args:
        field, wanted = item.split("=", 1)
        have = data
        for part in field.split("."):
            have = have.get(part) if isinstance(have, dict) else None
        wanted = json.loads(wanted)
        if isinstance(wanted, list):
            have = sorted(have or [])
            wanted = sorted(wanted)
        if have != wanted:
            sys.exit(1)
else:
    sys.exit(f"unknown helper {cmd}")
PY
)"
py() { python3 -c "$py_helper" "$@"; }

token=""
load_token() {
  if [[ -n "${BAO_TOKEN:-}" ]]; then
    token="$BAO_TOKEN"
    return
  fi
  [[ -n "$key_file" ]] || die "set BAO_TOKEN or pass --key-file"
  [[ -f "$key_file" ]] || die "no key file at $key_file"
  token="$(py root-token "$key_file")"
}

server_pods() {
  "${kube[@]}" -n "$namespace" get pods -l app.kubernetes.io/name=openbao,component=server \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort
}

target=""
pick_target() {
  if [[ -n "$addr" ]]; then
    target=""
  else
    target="$(server_pods | head -n 1)"
    [[ -n "$target" ]] || die "no OpenBao server pods in namespace $namespace; run helm/prerequisites/install.sh first"
  fi
}

# bao_in POD ARGS...: run the bao CLI with stdin as the payload. In the cluster
# it runs inside the server pod; the token travels as the first stdin line.
bao_in() {
  local pod="$1"
  shift
  if [[ -n "$addr" ]]; then
    BAO_ADDR="$addr" BAO_TOKEN="$token" bao "$@"
  else
    { printf '%s\n' "$token"; cat; } | "${kube[@]}" -n "$namespace" exec -i "$pod" -c openbao -- \
      sh -c 'IFS= read -r t; if [ -n "$t" ]; then BAO_TOKEN="$t"; export BAO_TOKEN; fi; exec bao "$@"' sh "$@"
  fi
}
bao_q() { bao_in "$@" </dev/null; }

# read_or_missing POD PATH: prints the JSON response, or nothing when the path
# holds no value. Any other error stops the script.
read_or_missing() {
  local out
  if out="$(bao_q "$1" read -format=json "$2" 2>&1)"; then
    printf '%s' "$out"
  elif [[ "$out" == *"No value found"* ]]; then
    return 0
  else
    die "reading $2 failed: $out"
  fi
}

seal_state() { # seal_state POD -> "initialized sealed"
  local out="" try
  # A server whose container just started may not listen yet.
  for try in 1 2 3 4 5 6 7 8 9 10; do
    out="$(bao_q "$1" status -format=json 2>/dev/null)" || true
    [[ -n "$out" ]] && break
    sleep "${OPENBAO_BOOTSTRAP_POLL_SECONDS:-3}"
  done
  [[ -n "$out" ]] || die "$1: bao status returned nothing after $try tries"
  printf '%s %s\n' "$(printf '%s' "$out" | py get initialized)" "$(printf '%s' "$out" | py get sealed)"
}

# wait_running: every server the StatefulSet wants exists and runs. Sealed
# servers are never Ready, so this waits for Running, not Ready.
wait_running() {
  local want running deadline=$((SECONDS + ${OPENBAO_BOOTSTRAP_WAIT_SECONDS:-300}))
  want="$("${kube[@]}" -n "$namespace" get statefulset openbao -o jsonpath='{.spec.replicas}')" \
    || die "no OpenBao StatefulSet in namespace $namespace; run helm/prerequisites/install.sh first"
  while true; do
    running="$("${kube[@]}" -n "$namespace" get pods -l app.kubernetes.io/name=openbao,component=server \
      --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -c . || true)"
    [[ "$running" -ge "$want" ]] && return 0
    (( SECONDS < deadline )) || die "$running of $want OpenBao servers are running; check: kubectl -n $namespace get pods"
    sleep "${OPENBAO_BOOTSTRAP_POLL_SECONDS:-3}"
  done
}

cmd_init() {
  [[ -z "$addr" ]] || die "init manages the in-cluster servers; an OpenBao you run yourself is initialized by you"
  [[ -n "$key_file" ]] || die "init needs --key-file PATH for the key shares"
  wait_running
  pick_target
  local state
  state="$(seal_state "$target")"
  if [[ "${state%% *}" == true ]]; then
    if [[ -f "$key_file" ]]; then
      log "already initialized; $key_file kept as it is"
    else
      log "already initialized and there is no key file at $key_file: unseal needs the shares you hold"
    fi
  else
    [[ ! -e "$key_file" ]] || die "$key_file exists; refusing to overwrite key material. Move it away or pick another path."
    local dir
    dir="$(dirname "$key_file")"
    [[ -d "$dir" ]] || die "directory $dir does not exist"
    # noclobber: the file is created only if it does not exist yet.
    ( umask 077; set -C; : >"$key_file" ) || die "could not create $key_file"
    chmod 600 "$key_file"
    if ! bao_q "$target" operator init -format=json -key-shares="$key_shares" -key-threshold="$key_threshold" >>"$key_file"; then
      die "operator init failed; inspect $key_file before retrying"
    fi
    py init-ok "$key_file" "$key_shares" || die "$key_file does not hold $key_shares key shares and a root token"
    log "initialized: $key_shares key shares (threshold $key_threshold) and the root token written to $key_file (mode 600)"
    log "hand the shares to their holders and keep that file off the cluster"
  fi
  [[ -f "$key_file" ]] && cmd_unseal
  return 0
}

cmd_unseal() {
  [[ -z "$addr" ]] || die "unseal manages the in-cluster servers; unseal an OpenBao you run yourself with your own runbook"
  [[ -n "$key_file" && -f "$key_file" ]] || die "unseal needs --key-file with the key shares"
  py private "$key_file" || die "$key_file is readable by group or others; chmod 600 it"
  local threshold
  threshold="$(py get unseal_threshold <"$key_file" 2>/dev/null || true)"
  [[ "$threshold" =~ ^[0-9]+$ ]] || threshold="$key_threshold"
  wait_running
  local pod state attempt i
  # The first server first: the others join its Raft cluster when unsealed.
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    state="$(seal_state "$pod")"
    if [[ "${state##* }" == false ]]; then
      log "$pod is unsealed"
      continue
    fi
    for attempt in 1 2 3 4 5 6; do
      for ((i = 0; i < threshold; i++)); do
        py unseal-key "$key_file" "$i" | bao_in "$pod" write -format=json sys/unseal key=- >/dev/null 2>&1 || true
      done
      sleep "${OPENBAO_BOOTSTRAP_POLL_SECONDS:-5}"
      state="$(seal_state "$pod")"
      [[ "${state##* }" == false ]] && break
      log "$pod is still sealed (attempt $attempt); retrying while it joins the cluster"
    done
    [[ "${state##* }" == false ]] || die "$pod stayed sealed after 6 attempts; check: kubectl -n $namespace logs $pod"
    log "$pod unsealed"
  done < <(server_pods)
}

ensure_mount() { # ensure_mount KIND PATH TYPE [enable args...]
  local kind="$1" path="$2" type="$3" list have
  shift 3
  if [[ "$kind" == auth ]]; then list="$(bao_q "$target" auth list -format=json)"; else list="$(bao_q "$target" secrets list -format=json)"; fi
  have="$(printf '%s' "$list" | py get "$path/" type 2>/dev/null || true)"
  if [[ -z "$have" ]]; then
    bao_q "$target" "$kind" enable -path="$path" "$@" "$type" >/dev/null
    log "enabled $type at $path/"
  elif [[ "$have" != "$type" ]]; then
    die "$path/ is a $have mount, not $type; pick another path"
  else
    log "$path/ ($type) exists"
  fi
}

policy_platform() {
  echo "# External Secrets Operator in $release_ns: read the platform secrets only."
  local p
  for p in "${platform_paths[@]}"; do
    printf 'path "%s/data/%s" {\n  capabilities = ["read"]\n}\n' "$kv_mount" "$p"
  done
}
policy_workspace() {
  echo "# External Secrets Operator in $workspace_ns: workload secrets and the model reader."
  local p
  for p in "${workspace_paths[@]}"; do
    printf 'path "%s/data/%s" {\n  capabilities = ["read"]\n}\n' "$kv_mount" "$p"
  done
}
policy_gateway() {
  echo "# The gateway: encrypt and decrypt with its transit key and renew its own token, nothing else."
  printf 'path "%s/encrypt/%s" {\n  capabilities = ["update"]\n}\n' "$transit_mount" "$transit_key"
  printf 'path "%s/decrypt/%s" {\n  capabilities = ["update"]\n}\n' "$transit_mount" "$transit_key"
  printf 'path "auth/token/renew-self" {\n  capabilities = ["update"]\n}\n'
}
policy_gateway_database() {
  echo "# The gateway: dynamic PostgreSQL credentials from its database role."
  printf 'path "%s/creds/%s" {\n  capabilities = ["read"]\n}\n' "$db_mount" "$release-gateway"
}

ensure_policy() { # ensure_policy NAME TEXT
  local name="$1" text="$2" current
  current="$(read_or_missing "$target" "sys/policies/acl/$name" | py get data policy 2>/dev/null || true)"
  if [[ "$current" == "$text" ]]; then
    log "policy $name is current"
  else
    printf '%s\n' "$text" | bao_in "$target" policy write "$name" - >/dev/null
    log "policy $name written"
  fi
}

ensure_role() { # ensure_role NAME SA NAMESPACE POLICIES(csv) AUDIENCE TTL MAX_TTL
  local name="$1" sa="$2" ns="$3" policies="$4" aud="$5" ttl="$6" max_ttl="$7" current
  local policies_json
  policies_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split(",")))' "$policies")"
  current="$(read_or_missing "$target" "auth/$auth_mount/role/$name")"
  if [[ -n "$current" ]] && printf '%s' "$current" | py same "bound_service_account_names=[\"$sa\"]" \
      "bound_service_account_namespaces=[\"$ns\"]" "token_policies=$policies_json" "audience=\"$aud\"" "token_ttl=$ttl" \
      "token_max_ttl=$max_ttl"; then
    log "role $name is current"
    return
  fi
  bao_q "$target" write "auth/$auth_mount/role/$name" bound_service_account_names="$sa" bound_service_account_namespaces="$ns" \
    token_policies="$policies" audience="$aud" token_ttl="$ttl" token_max_ttl="$max_ttl" >/dev/null
  log "role $name written ($sa in $ns, policies $policies)"
}

ensure_kubernetes_config() {
  local current want_ca=""
  current="$(read_or_missing "$target" "auth/$auth_mount/config")"
  if [[ -n "$kube_ca_file" ]]; then
    [[ -f "$kube_ca_file" ]] || die "no file $kube_ca_file"
    want_ca="$(cat "$kube_ca_file")"
  fi
  local fields=("kubernetes_host=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$kube_host")")
  [[ -n "$want_ca" ]] && fields+=("kubernetes_ca_cert=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$want_ca")")
  if [[ -n "$current" ]] && printf '%s' "$current" | py same "${fields[@]}"; then
    log "Kubernetes auth config is current"
    return
  fi
  if [[ -n "$addr" ]]; then
    local args=("auth/$auth_mount/config" "kubernetes_host=$kube_host")
    [[ -n "$kube_ca_file" ]] && args+=("kubernetes_ca_cert=@$kube_ca_file")
    if [[ -n "$reviewer_jwt_file" ]]; then
      [[ -f "$reviewer_jwt_file" ]] || die "no file $reviewer_jwt_file"
      printf '%s' "$(cat "$reviewer_jwt_file")" | bao_in "" write "${args[@]}" token_reviewer_jwt=- >/dev/null
    else
      bao_q "" write "${args[@]}" >/dev/null
    fi
  else
    # In the cluster OpenBao reviews tokens with its own service account
    # (the chart's auth-delegator binding) and the in-pod CA.
    bao_q "$target" write "auth/$auth_mount/config" kubernetes_host="$kube_host" >/dev/null
  fi
  log "Kubernetes auth config written ($kube_host)"
}

# The gateway passes each record's context to transit and refuses a key that
# is not derived. derived cannot change after creation, so a wrong key stops here.
ensure_transit_key() {
  local current
  current="$(read_or_missing "$target" "$transit_mount/keys/$transit_key")"
  if [[ -z "$current" ]]; then
    bao_q "$target" write -f "$transit_mount/keys/$transit_key" type=aes256-gcm96 derived=true >/dev/null
    log "transit key $transit_key created (aes256-gcm96, derived)"
  elif printf '%s' "$current" | py same "derived=true" "type=\"aes256-gcm96\""; then
    log "transit key $transit_key exists (derived)"
  else
    die "transit key $transit_key exists but is not a derived aes256-gcm96 key; the gateway refuses it. Create a new key name and pass it to the gateway."
  fi
}

ensure_database() {
  [[ "$db_url" == *"{{username}}"* && "$db_url" == *"{{password}}"* ]] || die "--database-connection-url must contain {{username}} and {{password}}"
  [[ "$db_url" =~ ^postgres(ql)?:// ]] || die "--database-connection-url must be a postgresql:// URL"
  [[ "$db_admin_user" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "--database-admin-user must be a plain role name"
  [[ -f "$db_admin_password_file" ]] || die "--database-admin-password-file must name a file"
  ensure_mount secrets "$db_mount" database
  local role="$release-gateway" current
  current="$(read_or_missing "$target" "$db_mount/config/$db_config")"
  local url_json
  url_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$db_url")"
  if [[ -n "$current" ]] && printf '%s' "$current" | py same "plugin_name=\"postgresql-database-plugin\"" \
      "allowed_roles=[\"$role\"]" "connection_details.connection_url=$url_json" "connection_details.username=\"$db_admin_user\""; then
    log "database connection $db_config is current"
  else
    printf '%s' "$(cat "$db_admin_password_file")" | bao_in "$target" write "$db_mount/config/$db_config" \
      plugin_name=postgresql-database-plugin allowed_roles="$role" connection_url="$db_url" username="$db_admin_user" password=- >/dev/null
    log "database connection $db_config written"
  fi
  local create="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE \"$db_group_role\";"
  local create_json
  create_json="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$create")"
  current="$(read_or_missing "$target" "$db_mount/roles/$role")"
  if [[ -n "$current" ]] && printf '%s' "$current" | py same "db_name=\"$db_config\"" "creation_statements=$create_json" \
      "default_ttl=3600" "max_ttl=86400"; then
    log "database role $role is current"
  else
    bao_q "$target" write "$db_mount/roles/$role" db_name="$db_config" creation_statements="$create" default_ttl=1h max_ttl=24h >/dev/null
    log "database role $role written"
  fi
}

require_unsealed() {
  [[ -n "$addr" ]] && return 0
  local state
  state="$(seal_state "$target")"
  [[ "$state" == "true false" ]] || die "$target is not initialized and unsealed ($state); run init or unseal first"
}

cmd_configure() {
  load_token
  pick_target
  require_unsealed
  ensure_mount secrets "$kv_mount" kv -version=2
  local version
  version="$(bao_q "$target" secrets list -format=json | py get "$kv_mount/" options version 2>/dev/null || true)"
  [[ "$version" == 2 ]] || die "$kv_mount/ is KV version ${version:-1}; Kionga needs KV v2"
  ensure_mount secrets "$transit_mount" transit
  ensure_transit_key
  ensure_mount auth "$auth_mount" kubernetes
  ensure_kubernetes_config
  ensure_policy "$release-platform-read" "$(policy_platform)"
  ensure_policy "$release-workspace-read" "$(policy_workspace)"
  ensure_policy "$release-gateway-transit" "$(policy_gateway)"
  local gateway_policies="$release-gateway-transit"
  if [[ $with_database -eq 1 ]]; then
    ensure_database
    ensure_policy "$release-gateway-database" "$(policy_gateway_database)"
    gateway_policies+=",$release-gateway-database"
  fi
  ensure_role "$release-platform" "$platform_sa" "$release_ns" "$release-platform-read" "$audience" 1200 4800
  ensure_role "$release-workspace" "$platform_sa" "$workspace_ns" "$release-workspace-read" "$audience" 1200 4800
  ensure_role "$release-gateway" "$gateway_sa" "$release_ns" "$gateway_policies" "$gateway_audience" 3600 86400
  log "configured: KV v2 at $kv_mount/, transit key $transit_key, roles $release-platform, $release-workspace, $release-gateway"
}

# seed ------------------------------------------------------------------------

valid_path() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?(/[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$ ]]; }
valid_key() { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; }

existing_keys() { # existing_keys PATH -> key names (one per line), never values
  local out
  if out="$(bao_q "$target" kv get -format=json -mount="$kv_mount" "$1" 2>&1)"; then
    printf '%s' "$out" | py keys data data
  elif [[ "$out" == *"No value found"* ]]; then
    return 0
  else
    die "reading $kv_mount/$1 failed: $(printf '%s' "$out" | head -n 1)"
  fi
}

random_value() { # random_value hex:N|base64:N, no trailing newline
  local kind="${1%%:*}" size="${1#*:}"
  [[ "$size" =~ ^[0-9]+$ ]] && (( size >= 16 && size <= 128 )) || die "random size must be 16 to 128 bytes, got $1"
  case "$kind" in
    hex) openssl rand -hex "$size" | tr -d '\n' ;;
    base64) openssl rand -base64 "$size" | tr -d '\n' ;;
    *) die "random values are hex:N or base64:N, got $1" ;;
  esac
}

# write_key PATH KEY SOURCE MODE: SOURCE is file:F or random:SPEC; MODE is
# keep (write only when absent) or replace.
write_key() {
  local path="$1" key="$2" source="$3" mode="$4" have
  valid_path "$path" || die "invalid KV path $path"
  valid_key "$key" || die "invalid key name $key"
  have="$(existing_keys "$path")"
  if [[ "$mode" == keep ]] && grep -Fxq "$key" <<<"$have"; then
    log "$kv_mount/$path $key exists; kept"
    return
  fi
  local verb=put value
  [[ -n "$have" ]] && verb="patch"
  # Produce the value before writing, so a failure never stores an empty one.
  case "$source" in
    file:*) value="$(cat "${source#file:}")" || die "could not read ${source#file:}" ;;
    random:*) value="$(random_value "${source#random:}")" || exit 1 ;;
  esac
  [[ -n "$value" ]] || die "refusing to write an empty value to $kv_mount/$path $key"
  printf '%s' "$value" | bao_in "$target" kv "$verb" -mount="$kv_mount" "$path" "$key=-" >/dev/null
  value=""
  log "$kv_mount/$path $key written (${source%%:*})"
}

split_spec() { # split_spec PATH/KEY=SPEC -> "PATH KEY SPEC"
  local spec="$1" target_part="${1%%=*}" value="${1#*=}"
  [[ "$spec" == *=* && "$target_part" == */* ]] || die "expected PATH/KEY=hex:N or base64:N, got $spec"
  printf '%s %s %s\n' "${target_part%/*}" "${target_part##*/}" "$value"
}

cmd_seed() {
  [[ -n "$from_dir" || ${#generate[@]} -gt 0 || ${#rotate[@]} -gt 0 ]] || die "seed needs --from-dir, --generate or --rotate"
  command -v openssl >/dev/null || die "openssl is required"
  load_token
  pick_target
  require_unsealed
  if [[ -n "$from_dir" ]]; then
    [[ -d "$from_dir" ]] || die "no directory $from_dir"
    local file rel mode=keep
    [[ $overwrite -eq 1 ]] && mode=replace
    while IFS= read -r file; do
      rel="${file#"$from_dir"/}"
      [[ "$rel" == */* ]] || die "$file: put files at <path>/<KEY>, for example gateway/DATABASE_URL"
      write_key "${rel%/*}" "${rel##*/}" "file:$file" "$mode"
    done < <(find "$from_dir" -type f ! -name '.*' | LC_ALL=C sort)
  fi
  local spec path key value
  # Check every spec before the first write.
  for spec in "${generate[@]+"${generate[@]}"}" "${rotate[@]+"${rotate[@]}"}"; do
    read -r path key value <<<"$(split_spec "$spec")"
    valid_path "$path" || die "invalid KV path $path"
    valid_key "$key" || die "invalid key name $key"
    random_value "$value" >/dev/null
  done
  for spec in "${generate[@]+"${generate[@]}"}"; do
    read -r path key value <<<"$(split_spec "$spec")"
    write_key "$path" "$key" "random:$value" keep
  done
  for spec in "${rotate[@]+"${rotate[@]}"}"; do
    read -r path key value <<<"$(split_spec "$spec")"
    write_key "$path" "$key" "random:$value" replace
  done
}

cmd_ca() {
  [[ -z "$addr" ]] || die "ca reads the in-cluster CA; with --addr use your OpenBao's CA certificate"
  [[ -n "$ca_out" ]] || die "ca needs --ca-out FILE"
  local pem
  pem="$("${kube[@]}" -n "$namespace" get secret openbao-server-tls -o jsonpath='{.data.ca\.crt}' | python3 -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()).decode())')"
  [[ "$pem" == "-----BEGIN CERTIFICATE-----"* ]] || die "openbao-server-tls in $namespace holds no CA certificate"
  printf '%s\n' "$pem" >"$ca_out"
  log "CA certificate written to $ca_out; pass it to the chart: --set-file secretStore.openbao.caBundle=$ca_out"
}

cmd_reload_tls() {
  [[ -z "$addr" ]] || die "reload-tls manages the in-cluster servers"
  local pod
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    "${kube[@]}" -n "$namespace" exec "$pod" -c openbao -- sh -c 'kill -HUP "$(pidof bao)"'
    log "$pod reloads its certificate"
  done < <(server_pods)
}

cmd_status() {
  if [[ -n "$addr" ]]; then
    local out
    out="$(BAO_ADDR="$addr" bao status -format=json 2>/dev/null)" || true
    echo "$addr initialized=$(printf '%s' "$out" | py get initialized) sealed=$(printf '%s' "$out" | py get sealed)"
    return
  fi
  local pod state
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    state="$(seal_state "$pod")"
    echo "$pod initialized=${state%% *} sealed=${state##* }"
  done < <(server_pods)
}

case "$command" in
  all)
    # init initializes only an uninitialized cluster, then unseals.
    if [[ -z "$addr" ]]; then cmd_init; fi
    cmd_configure
    if [[ -n "$ca_out" ]]; then cmd_ca; fi
    ;;
  init) cmd_init ;;
  unseal) cmd_unseal ;;
  configure) cmd_configure ;;
  seed) cmd_seed ;;
  ca) cmd_ca ;;
  reload-tls) cmd_reload_tls ;;
  status) cmd_status ;;
esac
