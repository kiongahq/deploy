"""scripts/openbao-bootstrap.sh against stub `bao` and `kubectl` commands
(tests/openbao_stub.py) on PATH: init writes the key shares once and never
overwrites them, every step is idempotent, policies are least privilege, and
no secret value reaches the output, a command line or the log."""

import json
import os
import stat
import subprocess
import sys

import pytest

from test_production_deploy import ROOT

SCRIPT = ROOT / "scripts/openbao-bootstrap.sh"
STUB = ROOT / "tests/openbao_stub.py"
PODS = ["openbao-0", "openbao-1", "openbao-2"]
MUTATING = (("operator", "init"), ("write",), ("secrets", "enable"), ("auth", "enable"), ("policy", "write"),
            ("kv", "put"), ("kv", "patch"))


class Cluster:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("bao", "kubectl"):
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + STUB.read_text())
            tool.chmod(0o755)
        self.state_file = tmp_path / "state.json"
        self.log_file = tmp_path / "calls.log"
        self.state_file.write_text(json.dumps({
            "pods": PODS, "initialized": False, "sealed": {p: True for p in PODS}, "joined": {}, "progress": {},
            "mounts": {"sys/": {"type": "system"}, "cubbyhole/": {"type": "cubbyhole"}}, "auths": {"token/": {"type": "token"}},
            "objects": {}, "kv": {}}))
        self.keys = tmp_path / "keys" / "openbao-init.json"
        self.keys.parent.mkdir()

    @property
    def state(self):
        return json.loads(self.state_file.read_text())

    def calls(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def reset_log(self):
        self.log_file.write_text("")

    def run(self, *args, env=None):
        environment = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp),
                       "BAO_STUB_STATE": str(self.state_file), "BAO_STUB_LOG": str(self.log_file),
                       "OPENBAO_BOOTSTRAP_POLL_SECONDS": "0"}
        environment.update(env or {})
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL)

    def mutations(self):
        found = []
        for call in self.calls():
            if call["tool"] != "bao":
                continue
            words = [a for a in call["argv"] if not a.startswith("-")]
            if any(tuple(words[:len(m)]) == m for m in MUTATING):
                found.append(words)
        return found

    def secret_values(self):
        state = self.state
        values = list(state.get("keys", [])) + ([state["root"]] if state.get("root") else [])
        for data in state["kv"].values():
            values += list(data.values())
        return [v for v in values if v]


@pytest.fixture
def cluster(tmp_path):
    return Cluster(tmp_path)


@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory):
    """One initialized, unsealed and configured cluster; tests start from a copy."""
    template = Cluster(tmp_path_factory.mktemp("template"))
    result = template.run("all", "--key-file", str(template.keys))
    assert result.returncode == 0, result.stderr
    return template


@pytest.fixture
def ready(tmp_path, bootstrapped):
    cluster = Cluster(tmp_path)
    cluster.state_file.write_text(bootstrapped.state_file.read_text())
    cluster.keys.write_text(bootstrapped.keys.read_text())
    cluster.keys.chmod(0o600)
    return cluster


def assert_no_leak(cluster, *results, extra=()):
    texts = [r.stdout + r.stderr for r in results]
    argv = json.dumps([c["argv"] for c in cluster.calls()])
    for value in list(cluster.secret_values()) + list(extra):
        for text in texts:
            assert value not in text, "a secret value reached the output"
        assert value not in argv, "a secret value reached a command line"


def bootstrap(cluster, *extra):
    result = cluster.run("all", "--key-file", str(cluster.keys), *extra)
    assert result.returncode == 0, result.stderr
    return result


# init and unseal ---------------------------------------------------------------

def test_all_initializes_once_writes_a_private_key_file_and_unseals_every_server(cluster):
    result = bootstrap(cluster)
    state = cluster.state
    assert state["initialized"] and not any(state["sealed"].values())
    assert stat.S_IMODE(cluster.keys.stat().st_mode) == 0o600
    data = json.loads(cluster.keys.read_text())
    assert len(data["unseal_keys_b64"]) == 5 and data["unseal_threshold"] == 3 and data["root_token"] == state["root"]
    assert "refusing" not in result.stderr
    inits = [c for c in cluster.mutations() if c[:2] == ["operator", "init"]]
    assert len(inits) == 1
    assert_no_leak(cluster, result)
    # The token travels on stdin, never in arguments.
    authenticated = [c for c in cluster.calls() if c["tool"] == "bao" and c["argv"][0] in ("secrets", "auth", "policy", "read", "kv")]
    assert authenticated and all(c["token"] for c in authenticated)
    kubectl_argv = json.dumps([c["argv"] for c in cluster.calls() if c["tool"] == "kubectl"])
    assert cluster.state["root"] not in kubectl_argv


def test_a_second_run_changes_nothing(ready):
    cluster = ready
    before = cluster.state
    cluster.reset_log()
    result = bootstrap(cluster)
    assert cluster.mutations() == [], cluster.mutations()
    assert cluster.state == before
    assert "already initialized" in result.stderr and "is current" in result.stderr


def test_init_refuses_to_overwrite_an_existing_key_file(cluster):
    cluster.keys.write_text("precious shares from another cluster")
    result = cluster.run("init", "--key-file", str(cluster.keys))
    assert result.returncode != 0 and "refusing to overwrite" in result.stderr
    assert cluster.keys.read_text() == "precious shares from another cluster"
    assert not cluster.state["initialized"]
    assert not [c for c in cluster.mutations() if c[:2] == ["operator", "init"]]


def test_init_needs_a_key_file_in_an_existing_directory(cluster):
    assert "needs --key-file" in cluster.run("init").stderr
    result = cluster.run("init", "--key-file", str(cluster.tmp / "missing" / "keys.json"))
    assert result.returncode != 0 and "does not exist" in result.stderr
    assert not cluster.state["initialized"]


def test_unseal_refuses_a_key_file_others_can_read(ready):
    cluster = ready
    state = cluster.state
    state["sealed"] = {p: True for p in PODS}
    cluster.state_file.write_text(json.dumps(state))
    cluster.keys.chmod(0o644)
    result = cluster.run("unseal", "--key-file", str(cluster.keys))
    assert result.returncode != 0 and "readable by group or others" in result.stderr
    cluster.keys.chmod(0o600)
    result = cluster.run("unseal", "--key-file", str(cluster.keys))
    assert result.returncode == 0, result.stderr
    assert not any(cluster.state["sealed"].values())
    assert_no_leak(cluster, result)


def test_unseal_skips_servers_that_are_already_unsealed(ready):
    cluster = ready
    state = cluster.state
    state["sealed"]["openbao-1"] = True
    cluster.state_file.write_text(json.dumps(state))
    cluster.reset_log()
    result = cluster.run("unseal", "--key-file", str(cluster.keys))
    assert result.returncode == 0, result.stderr
    pods = {c["pod"] for c in cluster.calls() if c["tool"] == "bao" and c["argv"][:2] == ["write", "-format=json"]}
    assert pods == {"openbao-1"}


def test_configure_stops_on_a_sealed_server(ready):
    cluster = ready
    state = cluster.state
    state["sealed"]["openbao-0"] = True
    cluster.state_file.write_text(json.dumps(state))
    result = cluster.run("configure", "--key-file", str(cluster.keys))
    assert result.returncode != 0 and "run init or unseal first" in result.stderr


# configure -------------------------------------------------------------------

def policy(cluster, name):
    return cluster.state["objects"][f"sys/policies/acl/{name}"]["policy"]


def paths(text):
    return {line.split('"')[1] for line in text.splitlines() if line.startswith("path ")}


def test_mounts_transit_key_and_auth_are_created(ready):
    cluster = ready
    state = cluster.state
    assert state["mounts"]["kionga/"] == {"type": "kv", "options": {"version": "2"}}
    assert state["mounts"]["transit/"]["type"] == "transit"
    assert state["objects"]["transit/keys/kionga-credential"]["type"] == "aes256-gcm96"
    assert state["auths"]["kubernetes/"]["type"] == "kubernetes"
    assert state["objects"]["auth/kubernetes/config"] == {"kubernetes_host": "https://kubernetes.default.svc:443"}


def test_each_consumer_gets_one_least_privilege_policy(ready):
    cluster = ready
    platform = policy(cluster, "kionga-platform-read")
    assert paths(platform) == {f"kionga/data/{p}" for p in ("shared", "gateway", "operator", "worker", "features", "storage",
                                                             "tracing", "agent-designer", "sandbox", "secret-references")}
    assert paths(policy(cluster, "kionga-workspace-read")) == {"kionga/data/model-storage", "kionga/data/workloads/*"}
    assert paths(policy(cluster, "kionga-gateway-transit")) == {"transit/encrypt/kionga-credential", "transit/decrypt/kionga-credential"}
    for name in ("kionga-platform-read", "kionga-workspace-read"):
        assert policy(cluster, name).count('capabilities = ["read"]') == len(paths(policy(cluster, name)))
    assert policy(cluster, "kionga-gateway-transit").count('capabilities = ["update"]') == 2
    for name in ("kionga-platform-read", "kionga-workspace-read", "kionga-gateway-transit"):
        text = policy(cluster, name)
        assert "sudo" not in text and "delete" not in text and "list" not in text and "create" not in text
        assert not any(p.startswith("sys/") or p in ("*", "kionga/*") for p in paths(text))


def test_roles_bind_one_service_account_in_one_namespace(ready):
    cluster = ready
    objects = cluster.state["objects"]
    platform = objects["auth/kubernetes/role/kionga-platform"]
    assert platform["bound_service_account_names"] == ["kionga-openbao-reader"]
    assert platform["bound_service_account_namespaces"] == ["kionga-system"]
    assert platform["token_policies"] == ["kionga-platform-read"] and platform["audience"] == "openbao"
    workspace = objects["auth/kubernetes/role/kionga-workspace"]
    assert workspace["bound_service_account_namespaces"] == ["kionga-workloads"]
    assert workspace["token_policies"] == ["kionga-workspace-read"]
    gateway = objects["auth/kubernetes/role/kionga-gateway"]
    assert gateway["bound_service_account_names"] == ["kionga-gateway"] and gateway["token_policies"] == ["kionga-gateway-transit"]


def test_release_and_namespaces_flow_into_roles(cluster):
    bootstrap(cluster, "--release", "acme", "--release-namespace", "acme-system", "--workspace-namespace", "acme-apps")
    objects = cluster.state["objects"]
    assert objects["auth/kubernetes/role/acme-platform"]["bound_service_account_names"] == ["acme-openbao-reader"]
    assert objects["auth/kubernetes/role/acme-workspace"]["bound_service_account_namespaces"] == ["acme-apps"]
    assert objects["auth/kubernetes/role/acme-gateway"]["bound_service_account_names"] == ["acme-gateway"]


def test_a_drifted_policy_is_rewritten(ready):
    cluster = ready
    state = cluster.state
    state["objects"]["sys/policies/acl/kionga-platform-read"]["policy"] = 'path "kionga/*" { capabilities = ["read", "list"] }'
    cluster.state_file.write_text(json.dumps(state))
    cluster.reset_log()
    bootstrap(cluster)
    assert cluster.mutations() == [["policy", "write", "kionga-platform-read"]]
    assert "kionga/*" not in policy(cluster, "kionga-platform-read")


@pytest.mark.parametrize("args,message", [
    (["--release", 'x"bad'], "--release must be"),
    (["--namespace", "Open Bao"], "--namespace must be"),
    (["--key-shares", "2", "--key-threshold", "3"], "threshold"),
    (["--kubernetes-host", "http://insecure:80"], "https URL"),
    (["--token-reviewer-jwt-file", "/tmp/jwt"], "outside the cluster"),
])
def test_unsafe_options_are_refused(cluster, args, message):
    result = cluster.run("configure", *args)
    assert result.returncode != 0 and message in result.stderr
    assert cluster.mutations() == []


def test_unknown_command_and_argument(cluster):
    assert cluster.run("teleport").returncode == 2
    assert cluster.run("status", "--bogus").returncode == 2


# database ----------------------------------------------------------------------

def test_database_engine_is_optional_idempotent_and_keeps_the_password_off_the_command_line(ready):
    cluster = ready
    password = cluster.tmp / "pg-admin"
    password.write_text("pg-admin-password-value\n")
    url = "postgresql://{{username}}:{{password}}@postgres.kionga-data.svc:5432/kionga?sslmode=verify-full"
    args = ["--with-database", "--database-connection-url", url, "--database-admin-user", "kionga_admin",
            "--database-admin-password-file", str(password)]
    assert "database/" not in cluster.state["mounts"]
    result = cluster.run("configure", "--key-file", str(cluster.keys), *args)
    assert result.returncode == 0, result.stderr
    objects = cluster.state["objects"]
    assert objects["database/config/kionga-postgres"]["password_set"]
    assert objects["database/config/kionga-postgres"]["connection_details"]["connection_url"] == url
    assert 'IN ROLE "kionga"' in objects["database/roles/kionga-gateway"]["creation_statements"][0]
    assert objects["auth/kubernetes/role/kionga-gateway"]["token_policies"] == ["kionga-gateway-transit", "kionga-gateway-database"]
    assert paths(policy(cluster, "kionga-gateway-database")) == {"database/creds/kionga-gateway"}
    assert_no_leak(cluster, result, extra=["pg-admin-password-value"])
    cluster.reset_log()
    assert cluster.run("configure", "--key-file", str(cluster.keys), *args).returncode == 0
    assert cluster.mutations() == []


def test_database_needs_templated_connection_url(ready):
    cluster = ready
    password = cluster.tmp / "pg"
    password.write_text("x")
    result = cluster.run("configure", "--key-file", str(cluster.keys), "--with-database", "--database-connection-url",
                         "postgresql://admin:inline@db/kionga", "--database-admin-user", "admin", "--database-admin-password-file", str(password))
    assert result.returncode != 0 and "{{username}}" in result.stderr


# seed --------------------------------------------------------------------------

def test_seed_writes_files_and_random_values_without_printing_them(ready):
    cluster = ready
    source = cluster.tmp / "seed"
    (source / "gateway").mkdir(parents=True)
    (source / "gateway" / "DATABASE_URL").write_text("postgres://kionga:file-secret-value@db/kionga\n")
    (source / "workloads" / "billing").mkdir(parents=True)
    (source / "workloads" / "billing" / "API_TOKEN").write_text("workload-secret-value")
    result = cluster.run("seed", "--key-file", str(cluster.keys), "--from-dir", str(source),
                         "--generate", "shared/KIONGA_INTERNAL_TOKEN=hex:32", "--generate", "gateway/KIONGA_CREDENTIAL_KEY=base64:32")
    assert result.returncode == 0, result.stderr
    kv = cluster.state["kv"]
    assert kv["kionga/gateway"]["DATABASE_URL"] == "postgres://kionga:file-secret-value@db/kionga"
    assert len(kv["kionga/gateway"]["KIONGA_CREDENTIAL_KEY"]) == 44
    assert len(kv["kionga/shared"]["KIONGA_INTERNAL_TOKEN"]) == 64
    assert kv["kionga/workloads/billing"] == {"API_TOKEN": "workload-secret-value"}
    assert_no_leak(cluster, result)
    writes = [c for c in cluster.calls() if c["tool"] == "bao" and c["argv"][:1] == ["kv"] and c["argv"][1] in ("put", "patch")]
    assert writes and all(c["argv"][-1].endswith("=-") and c["stdin_bytes"] > 0 for c in writes)


def test_seed_keeps_existing_values_and_rotate_replaces_one(ready):
    cluster = ready
    seed = ["seed", "--key-file", str(cluster.keys), "--generate", "gateway/OIDC_CLIENT_SECRET=hex:24",
            "--generate", "gateway/KIONGA_LOCAL_PASSWORD=hex:24"]
    assert cluster.run(*seed).returncode == 0
    first = dict(cluster.state["kv"]["kionga/gateway"])
    cluster.reset_log()
    result = cluster.run(*seed)
    assert result.returncode == 0 and cluster.mutations() == [] and "kept" in result.stderr
    result = cluster.run("seed", "--key-file", str(cluster.keys), "--rotate", "gateway/OIDC_CLIENT_SECRET=hex:24")
    assert result.returncode == 0, result.stderr
    after = cluster.state["kv"]["kionga/gateway"]
    assert after["OIDC_CLIENT_SECRET"] != first["OIDC_CLIENT_SECRET"]
    assert after["KIONGA_LOCAL_PASSWORD"] == first["KIONGA_LOCAL_PASSWORD"]
    assert_no_leak(cluster, result, extra=[first["OIDC_CLIENT_SECRET"]])


def test_seed_overwrite_replaces_file_values(ready):
    cluster = ready
    source = cluster.tmp / "seed"
    (source / "features").mkdir(parents=True)
    (source / "features" / "REDIS_URL").write_text("redis://one")
    assert cluster.run("seed", "--key-file", str(cluster.keys), "--from-dir", str(source)).returncode == 0
    (source / "features" / "REDIS_URL").write_text("redis://two")
    assert cluster.run("seed", "--key-file", str(cluster.keys), "--from-dir", str(source)).returncode == 0
    assert cluster.state["kv"]["kionga/features"]["REDIS_URL"] == "redis://one"
    assert cluster.run("seed", "--key-file", str(cluster.keys), "--from-dir", str(source), "--overwrite").returncode == 0
    assert cluster.state["kv"]["kionga/features"]["REDIS_URL"] == "redis://two"


@pytest.mark.parametrize("spec,message", [
    ("gateway/KEY=hex:8", "16 to 128"), ("gateway/KEY=plain:32", "hex:N or base64:N"), ("KEY=hex:32", "PATH/KEY"),
    ("Gateway/KEY=hex:32", "invalid KV path"), ("gateway/1KEY=hex:32", "invalid key name"),
])
def test_seed_rejects_bad_specs(ready, spec, message):
    cluster = ready
    cluster.reset_log()
    result = cluster.run("seed", "--key-file", str(cluster.keys), "--generate", spec)
    assert result.returncode != 0 and message in result.stderr
    assert cluster.mutations() == []


def test_bao_token_overrides_the_key_file(ready):
    cluster = ready
    root = cluster.state["root"]
    result = cluster.run("seed", "--generate", "shared/KIONGA_INTERNAL_TOKEN=hex:32", env={"BAO_TOKEN": root})
    assert result.returncode == 0, result.stderr
    result = cluster.run("seed", "--generate", "shared/OTHER=hex:32", env={"BAO_TOKEN": "wrong"})
    assert result.returncode != 0


# ca, status and an OpenBao outside the cluster --------------------------------

def test_ca_writes_the_public_certificate(cluster):
    out = cluster.tmp / "openbao-ca.crt"
    result = cluster.run("ca", "--ca-out", str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert "secretStore.openbao.caBundle" in result.stderr


def test_status_prints_state_only(ready):
    cluster = ready
    result = cluster.run("status")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [f"{p} initialized=true sealed=false" for p in PODS]


def test_external_openbao_is_configured_with_the_local_cli_and_never_initialized(cluster):
    state = cluster.state
    state.update(initialized=True, root="external-admin-token", sealed={})
    cluster.state_file.write_text(json.dumps(state))
    jwt = cluster.tmp / "reviewer.jwt"
    jwt.write_text("reviewer-jwt-value")
    ca = cluster.tmp / "k8s-ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nk8s\n-----END CERTIFICATE-----\n")
    addr = ["--addr", "https://bao.example.com:8200"]
    assert "init manages the in-cluster" in cluster.run("init", "--key-file", str(cluster.keys), *addr).stderr
    result = cluster.run("configure", *addr, "--kubernetes-host", "https://api.cluster.example.com:6443",
                         "--kubernetes-ca-file", str(ca), "--token-reviewer-jwt-file", str(jwt), env={"BAO_TOKEN": "external-admin-token"})
    assert result.returncode == 0, result.stderr
    calls = [c for c in cluster.calls() if c["tool"] == "bao"]
    assert calls and all(c["pod"] == "external" and c["addr"] == "https://bao.example.com:8200" for c in calls)
    assert not any(c["tool"] == "kubectl" for c in cluster.calls())
    config = cluster.state["objects"]["auth/kubernetes/config"]
    assert config["kubernetes_host"] == "https://api.cluster.example.com:6443" and config["token_reviewer_jwt_set"]
    assert_no_leak(cluster, result, extra=["reviewer-jwt-value", "external-admin-token"])


def test_init_waits_for_every_server_to_run(cluster):
    state = cluster.state
    state["running"] = ["openbao-0"]
    cluster.state_file.write_text(json.dumps(state))
    result = cluster.run("init", "--key-file", str(cluster.keys), env={"OPENBAO_BOOTSTRAP_WAIT_SECONDS": "0"})
    assert result.returncode != 0 and "1 of 3 OpenBao servers are running" in result.stderr
    assert not cluster.state["initialized"] and not cluster.keys.exists()
