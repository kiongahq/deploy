"""Agent teams on the single-server install: off by default, enabled through
deployment.json, and held to the same rules as the public overlay. The unsafe flag and
any runtime other than runsc cannot be expressed in this path at all."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from test_production_deploy import ROOT, vm, vm_inputs

DIGEST = "@sha256:" + "c" * 64
UNSAFE = "KIONGA_SANDBOX_ALLOW_UNSAFE_RUNTIME"
TOKEN = "t" * 32


def enabled(sandbox=True):
    block = {"enabled": True, "agent_runtime_image": "registry/agent-runtime" + DIGEST,
             "trace_proxy_image": "registry/trace-proxy" + DIGEST,
             "llm_upstream_url": "https://api.openai.com", "llm_model": "gpt-x"}
    if sandbox:
        block["sandbox"] = {"enabled": True, "manager_image": "registry/sandbox-manager" + DIGEST,
                            "images": ["registry/sandbox" + DIGEST], "git_hosts": ["github.com"]}
    return block


def inputs(tmp_path, teams=None, skip=()):
    path, data = vm_inputs(tmp_path)
    if teams is not None:
        data["agent_teams"] = teams
    path.write_text(json.dumps(data))
    for name in (*vm.TEAM_SECRETS, *vm.SANDBOX_SECRETS):
        if name not in skip:
            secret = Path(data["secrets_directory"]) / name
            secret.write_text(TOKEN + "\n")
            secret.chmod(0o640)
    return path, data


def rendered(path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for VM configuration validation")
    result = subprocess.run([*vm.compose_command(path), "config", "--format", "json"], capture_output=True, text=True,
                            env=vm.deployment_environment(path))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def files(command):
    return [Path(command[i + 1]).name for i, part in enumerate(command) if part == "-f"]


def test_off_by_default_the_stack_is_unchanged(tmp_path):
    path, _ = inputs(tmp_path)
    assert files(vm.compose_command(path)) == ["compose.yaml"]
    env = vm.deployment_environment(path)
    assert not [name for name in env if "SANDBOX" in name or "LLM" in name or "TRACE_PROXY" in name]
    assert set(rendered(path)["services"]) == {"gateway", "caddy"}


def test_off_needs_no_team_secrets(tmp_path):
    path, _ = inputs(tmp_path, skip=(*vm.TEAM_SECRETS, *vm.SANDBOX_SECRETS))
    assert vm.deployment_environment(path)


def test_the_example_is_off_and_every_key_is_known():
    data = json.loads((ROOT / "vm/deployment.example.json").read_text())
    teams = data["agent_teams"]
    assert teams["enabled"] is False and teams["sandbox"]["enabled"] is False
    assert vm.team_settings(data) == ({}, {})
    assert set(teams) <= vm.TEAM_KEYS and set(teams["sandbox"]) <= vm.SANDBOX_KEYS


def test_teams_without_sandboxes_add_only_the_runner_and_proxy(tmp_path):
    path, _ = inputs(tmp_path, enabled(sandbox=False), skip=vm.SANDBOX_SECRETS)
    assert files(vm.compose_command(path)) == ["compose.yaml", "compose.teams.yaml"]
    services = rendered(path)["services"]
    assert set(services) == {"gateway", "caddy", "trace-proxy", "agent-designer-runner"}
    assert "KIONGA_SANDBOX_URL" not in services["gateway"]["environment"]


def test_only_the_manager_gets_an_egress_network_and_only_with_git_hosts(tmp_path):
    path, _ = inputs(tmp_path, enabled())
    assert files(vm.compose_command(path))[-1] == "compose.sandbox-egress.yaml"
    config = rendered(path)
    alone = {name for name, service in config["services"].items() if "sandbox-egress" in service["networks"]}
    assert alone == {"sandbox-manager"}
    assert not config["networks"]["sandbox-egress"].get("internal")
    teams = enabled()
    teams["sandbox"]["git_hosts"] = []
    path, _ = inputs(tmp_path / "none" if (tmp_path / "none").mkdir() is None else tmp_path, teams)
    assert "compose.sandbox-egress.yaml" not in files(vm.compose_command(path))
    assert set(rendered(path)["services"]["sandbox-manager"]["networks"]) == {"sandbox"}


def test_the_runner_reaches_the_gateway_and_models_only_through_the_proxy(tmp_path):
    path, _ = inputs(tmp_path, enabled())
    config = rendered(path)
    services = config["services"]
    gateway = services["gateway"]["environment"]
    assert gateway["KIONGA_AGENT_DESIGNER_RUNNER_URL"] == "http://agent-designer-runner:9100"
    assert gateway["KIONGA_AGENT_DESIGNER_CALLBACK_URL"] == "http://gateway:8087/api/v1"
    assert gateway["KIONGA_AGENT_DESIGNER_RUNNER_TOKEN_FILE"] == "/run/secrets/KIONGA_DESIGNER_RUNNER_TOKEN"
    runner = services["agent-designer-runner"]
    assert runner["environment"]["KIONGA_LLM_BASE_URL"] == "http://trace-proxy:8081/v1"
    assert set(runner["networks"]) == {"agents"}
    assert set(services["trace-proxy"]["networks"]) == {"agents", "llm-egress"}
    assert services["trace-proxy"]["environment"]["LLM_UPSTREAM_URL"] == "https://api.openai.com"
    nets = config["networks"]
    assert nets["agents"]["internal"] is True and nets["sandbox"]["internal"] is True
    assert not nets["llm-egress"].get("internal")


def test_only_the_gateway_joins_the_internal_networks_and_caddy_stays_out(tmp_path):
    path, _ = inputs(tmp_path, enabled())
    services = rendered(path)["services"]
    assert set(services["caddy"]["networks"]) == {"backend"}
    assert set(services["gateway"]["networks"]) == {"backend", "agents", "sandbox"}
    members = {name for name, service in services.items() if "sandbox" in service["networks"]}
    assert members == {"gateway", "sandbox-manager"}
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"caddy"}, "Caddy stays the only public listener"


def test_the_manager_is_pinned_to_gvisor_and_production_whatever_the_shell_says(tmp_path, monkeypatch):
    monkeypatch.setenv(UNSAFE, "1")
    monkeypatch.setenv("KIONGA_SANDBOX_RUNTIME", "runc")
    monkeypatch.setenv("KIONGA_ENVIRONMENT", "development")
    path, _ = inputs(tmp_path, enabled())
    assert UNSAFE not in vm.deployment_environment(path)
    manager = rendered(path)["services"]["sandbox-manager"]
    env = manager["environment"]
    assert env["KIONGA_SANDBOX_RUNTIME"] == "runsc"
    assert env["KIONGA_ENVIRONMENT"] == "production"
    assert env[UNSAFE] == ""
    assert manager["read_only"] is True and manager["cap_drop"] == ["ALL"]
    assert [v["target"] for v in manager["volumes"]] == ["/var/run/docker.sock"]


def test_the_overlay_cannot_interpolate_the_runtime_or_the_flag():
    text = (ROOT / "vm/compose.sandbox.yaml").read_text()
    env = yaml.safe_load(text)["services"]["sandbox-manager"]["environment"]
    for name in ("KIONGA_SANDBOX_RUNTIME", "KIONGA_ENVIRONMENT", UNSAFE, "KIONGA_SANDBOX_DRIVER"):
        assert "$" not in env[name], f"{name} must be a literal"
    assert "$" not in (ROOT / "vm/compose.sandbox.yaml").read_text().split("volumes:")[1].split("environment:")[0]


def test_the_socket_is_only_on_the_manager(tmp_path):
    path, _ = inputs(tmp_path, enabled())
    services = rendered(path)["services"]
    mounted = {name for name, service in services.items()
               if any(v.get("target") == "/var/run/docker.sock" for v in service.get("volumes", []))}
    assert mounted == {"sandbox-manager"}


def test_secrets_arrive_as_files_and_never_in_the_environment(tmp_path):
    path, _ = inputs(tmp_path, enabled())
    env = vm.deployment_environment(path)
    assert TOKEN not in str(env)
    config = rendered(path)
    assert TOKEN not in json.dumps(config)
    gateway, manager = config["services"]["gateway"], config["services"]["sandbox-manager"]
    assert gateway["environment"]["KIONGA_SANDBOX_TOKEN_FILE"] == manager["environment"]["KIONGA_SANDBOX_TOKEN_FILE"]
    assert "KIONGA_SANDBOX_TOKEN" not in gateway["environment"] and "KIONGA_SANDBOX_TOKEN" not in manager["environment"]
    assert {s["source"] for s in gateway["secrets"]} >= {"KIONGA_SANDBOX_TOKEN", "KIONGA_DESIGNER_RUNNER_TOKEN"}
    runner = yaml.safe_load((ROOT / "vm/compose.teams.yaml").read_text())["services"]["agent-designer-runner"]
    assert "OPENAI_API_KEY" not in runner["environment"]
    assert '$$(cat /run/secrets/TRACE_PROXY_PROJECT_KEY)' in runner["entrypoint"][2]


def test_every_new_container_is_hardened():
    for name in ("compose.teams.yaml", "compose.sandbox.yaml"):
        services = yaml.safe_load((ROOT / "vm" / name).read_text())["services"]
        for service, spec in services.items():
            if service == "gateway":
                continue
            assert spec["read_only"] is True and spec["cap_drop"] == ["ALL"], service
            assert "no-new-privileges:true" in spec["security_opt"] and spec["pids_limit"] and spec["mem_limit"], service
            assert "ports" not in spec, service
            assert "build" not in spec and spec["image"].startswith("${"), "released images only"


@pytest.mark.parametrize("change,message", [
    (lambda t: t.update(agent_runtime_image="registry/agent-runtime:latest"), "agent_runtime_image"),
    (lambda t: t.update(trace_proxy_image="registry/trace-proxy:1"), "trace_proxy_image"),
    (lambda t: t.update(llm_upstream_url="ftp://x"), "llm_upstream_url"),
    (lambda t: t.update(llm_upstream_url="https://user:pass@x"), "without credentials"),
    (lambda t: t.pop("llm_model"), "llm_model"),
    (lambda t: t.update(enabled="yes"), "true or false"),
    (lambda t: t.update(unsafe_runtime=True), "unknown settings"),
    (lambda t: t["sandbox"].update(manager_image="registry/m:latest"), "manager_image"),
    (lambda t: t["sandbox"].update(images=[]), "images"),
    (lambda t: t["sandbox"].update(images=["registry/sandbox:latest"]), "images"),
    (lambda t: t["sandbox"].update(git_hosts=["https://github.com"]), "git_hosts"),
    (lambda t: t["sandbox"].update(allow_unsafe_runtime=True), "unknown settings"),
    (lambda t: t["sandbox"].update(runtime="runc"), "unknown settings"),
])
def test_rejects_unsafe_team_settings(tmp_path, change, message):
    teams = enabled()
    change(teams)
    path, _ = inputs(tmp_path, teams)
    with pytest.raises(ValueError, match=message):
        vm.deployment_environment(path)


def test_sandbox_needs_teams(tmp_path):
    teams = enabled()
    teams["enabled"] = False
    path, _ = inputs(tmp_path, teams)
    with pytest.raises(ValueError, match="needs agent_teams.enabled"):
        vm.deployment_environment(path)


def test_unknown_settings_are_refused_even_while_off(tmp_path):
    path, _ = inputs(tmp_path, {"enabled": False, "sandbox": {"runtime": "runc"}})
    with pytest.raises(ValueError, match="unknown settings"):
        vm.deployment_environment(path)


@pytest.mark.parametrize("name", ["KIONGA_SANDBOX_TOKEN", "KIONGA_DESIGNER_RUNNER_TOKEN"])
def test_short_tokens_are_refused_without_printing_them(tmp_path, name):
    path, data = inputs(tmp_path, enabled())
    (Path(data["secrets_directory"]) / name).write_text("s" * 31 + "\n")
    with pytest.raises(ValueError, match="at least 32 characters") as failure:
        vm.deployment_environment(path)
    assert "sss" not in str(failure.value)


@pytest.mark.parametrize("name", [*("KIONGA_SANDBOX_TOKEN", "TRACE_PROXY_KEYS", "TRACE_PROXY_PROJECT_KEY", "LLM_UPSTREAM_API_KEY")])
def test_enabled_requires_each_secret_file(tmp_path, name):
    path, _ = inputs(tmp_path, enabled(), skip=(name,))
    with pytest.raises(ValueError, match=name):
        vm.deployment_environment(path)


def test_world_readable_sandbox_token_is_refused(tmp_path):
    path, data = inputs(tmp_path, enabled())
    (Path(data["secrets_directory"]) / "KIONGA_SANDBOX_TOKEN").chmod(0o644)
    with pytest.raises(ValueError, match="world-accessible"):
        vm.deployment_environment(path)


@pytest.mark.parametrize("key", [UNSAFE, "KIONGA_SANDBOX_RUNTIME"])
def test_the_config_file_cannot_carry_sandbox_settings(tmp_path, key):
    path, data = inputs(tmp_path, enabled())
    config = Path(data["config_file"])
    values = json.loads(config.read_text())
    values[key] = "1"
    config.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="sandbox settings"):
        vm.deployment_environment(path)


@pytest.mark.parametrize("name", ["KIONGA_DESIGNER_RUNNER_TOKEN", "KIONGA_SANDBOX_TOKEN", "TRACE_PROXY_PROJECT_KEY"])
def test_development_tokens_are_refused_whatever_their_length(tmp_path, name):
    # The local default for the runner token is long enough to pass a length check.
    path, data = inputs(tmp_path, enabled())
    (Path(data["secrets_directory"]) / name).write_text("-".join(["kionga", "LOCAL", "designer", "runner", "token"]))
    with pytest.raises(ValueError, match="development value"):
        vm.deployment_environment(path)


def preflight_defaults():
    out = subprocess.run(["sh", str(ROOT / "scripts/public-preflight.sh"), "--list"], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.split())


@pytest.mark.parametrize("teams", [None, enabled(sandbox=False), enabled()], ids=["base", "teams", "sandbox"])
def test_no_mode_has_a_local_default_or_a_second_public_port(tmp_path, teams):
    # What deploy#52 fixed for the legacy overlay, checked on this path.
    path, _ = inputs(tmp_path, teams)
    config = rendered(path)
    text = json.dumps(config)
    for name, default in preflight_defaults().items():
        assert default not in text, f"the stack carries the local default of {name}"
    published = [(name, port["published"]) for name, service in config["services"].items() for port in service.get("ports", [])]
    assert sorted(published) == [("caddy", "443"), ("caddy", "80")]
    assert config["services"]["gateway"]["environment"]["KIONGA_ENVIRONMENT"] == "production"


def test_files_have_no_defaulted_credentials():
    # A ${VAR:-default} on a credential is how the local stack got its defaults.
    for name in ("compose.yaml", "compose.teams.yaml", "compose.sandbox.yaml"):
        for variable, default in re.findall(r"\$\{([A-Z0-9_]+):-([^}]*)\}", (ROOT / "vm" / name).read_text()):
            assert default == "", f"{name}: {variable} has a default"


def test_config_example_carries_no_secret():
    values = json.loads((ROOT / "vm/config.example.json").read_text())
    assert not [key for key in values if re.search("PASSWORD|SECRET|API_KEY", key) or key in ("KIONGA_INTERNAL_TOKEN",)]
    assert values["KIONGA_ENVIRONMENT"] == "production"
    assert "KIONGA_LOCAL_LOGIN" not in values


def test_each_stack_gives_each_service_exactly_one_token_form(tmp_path):
    # runtimeconfig.Load stops the process when a secret is set both ways.
    path, _ = inputs(tmp_path, enabled())
    file_form = rendered(path)["services"]
    local = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for service in ("gateway", "sandbox-manager"):
        assert "KIONGA_SANDBOX_TOKEN" in local[service]["environment"]
        assert "KIONGA_SANDBOX_TOKEN_FILE" not in local[service]["environment"]
        assert "KIONGA_SANDBOX_TOKEN_FILE" in file_form[service]["environment"]
        assert "KIONGA_SANDBOX_TOKEN" not in file_form[service]["environment"]


def test_platform_accepts_both_token_forms_once_the_manager_exists():
    # The local stack and Helm pass the value; the single server passes a file, which
    # is read only for names in runtimeconfig.SecretNames. Without the name, the
    # manager refuses to start and the gateway reports sandbox_unavailable.
    platform = ROOT.parent / "platform" / "go"
    if not (platform / "cmd/sandbox-manager").is_dir():
        pytest.skip("the sandbox manager is not in this platform checkout yet")
    assert '"KIONGA_SANDBOX_TOKEN"' in (platform / "internal/runtimeconfig/config.go").read_text()
    assert 'getenv("KIONGA_SANDBOX_TOKEN")' in (platform / "internal/sandbox/config.go").read_text()
    assert "runtimeconfig.Load()" in (platform / "cmd/sandbox-manager/main.go").read_text()
    assert "runtimeconfig.Load()" in (platform / "cmd/gateway/main.go").read_text()


TUNING = {"trace_proxy_write_timeout_seconds": 900, "turn_timeout_seconds": 300, "compact_threshold_tokens": 200000, "worker_pool": 8,
          "sandbox_retention_seconds": 3600, "scheduler": "off"}


def test_team_tuning_reaches_the_gateway_as_variables(tmp_path):
    teams = enabled(sandbox=False)
    teams["tuning"] = TUNING
    path, _ = inputs(tmp_path, teams)
    gateway = rendered(path)["services"]["gateway"]["environment"]
    assert gateway["KIONGA_AGENT_TURN_TIMEOUT_SECONDS"] == "300"
    assert gateway["KIONGA_TEAM_COMPACT_THRESHOLD_TOKENS"] == "200000"
    assert gateway["KIONGA_TEAM_WORKER_POOL"] == "8"
    assert gateway["KIONGA_TEAM_SANDBOX_RETENTION_SECONDS"] == "3600"
    assert gateway["KIONGA_TEAM_SCHEDULER"] == "off"
    proxy = rendered(path)["services"]["trace-proxy"]["environment"]
    assert proxy["TRACE_PROXY_WRITE_TIMEOUT_SECONDS"] == "900"


def test_team_tuning_is_empty_unless_set(tmp_path):
    path, _ = inputs(tmp_path, enabled(sandbox=False))
    gateway = rendered(path)["services"]["gateway"]["environment"]
    for variable in (*vm.TEAM_TUNING.values(), "KIONGA_TEAM_SCHEDULER"):
        if variable != "TRACE_PROXY_WRITE_TIMEOUT_SECONDS":
            assert gateway[variable] == "", "the gateway treats empty as its default"


@pytest.mark.parametrize("tuning,message", [
    ({"worker_pool": 0}, "positive whole number"),
    ({"trace_proxy_write_timeout_seconds": 3601}, "from 1 to 3600"),
    ({"worker_pool": "8"}, "positive whole number"),
    ({"worker_pool": True}, "positive whole number"),
    ({"scheduler": "on"}, "can only be off"),
    ({"allow_unsafe": True}, "accepts only"),
])
def test_bad_tuning_is_refused(tmp_path, tuning, message):
    teams = enabled(sandbox=False)
    teams["tuning"] = tuning
    path, _ = inputs(tmp_path, teams)
    with pytest.raises(ValueError, match=message):
        vm.deployment_environment(path)


def with_references(tmp_path, references, write=()):
    path, data = inputs(tmp_path)
    data["secret_references"] = references
    path.write_text(json.dumps(data))
    for name in write:
        secret = Path(data["secrets_directory"]) / name
        secret.write_text("reference-value\n")
        secret.chmod(0o640)
    return path


def test_no_references_add_no_compose_file(tmp_path):
    path = with_references(tmp_path, {"files": [], "knowledge_allowlist": [], "connection_allowlist": []})
    assert files(vm.compose_command(path)) == ["compose.yaml"]


def test_reference_files_reach_only_the_gateway_as_file_secrets(tmp_path):
    names = ["KIONGA_KB_SECRET_DOCS", "KIONGA_CONNECTION_SECRET_GITHUB", "KIONGA_QDRANT_KEY"]
    path = with_references(tmp_path, {"files": names}, write=names)
    vm.deployment_environment(path)
    command = vm.compose_command(path)
    assert "reference-value" not in Path(command[-1]).read_text(), "the overlay holds names and paths only"
    config = rendered(path)
    gateway, caddy = config["services"]["gateway"], config["services"]["caddy"]
    for name in names:
        assert gateway["environment"][f"{name}_FILE"] == f"/run/secrets/{name}"
        assert name not in gateway["environment"]
    assert {s["source"] for s in gateway["secrets"]} >= set(names)
    assert "secrets" not in caddy
    assert "reference-value" not in json.dumps(config)


def test_allowlists_become_the_gateway_settings(tmp_path):
    path = with_references(tmp_path, {"files": ["OLD_QDRANT"], "knowledge_allowlist": ["OLD_QDRANT", "OTHER"],
                                      "connection_allowlist": ["OLD_API"]}, write=["OLD_QDRANT"])
    gateway = rendered(path)["services"]["gateway"]["environment"]
    assert gateway["KIONGA_KB_SECRET_REFS"] == "OLD_QDRANT,OTHER"
    assert gateway["KIONGA_CONNECTION_SECRET_REFS"] == "OLD_API"
    assert gateway["OLD_QDRANT_FILE"] == "/run/secrets/OLD_QDRANT"


def test_every_scope_reaches_the_gateway_as_a_file(tmp_path):
    names = ["KIONGA_FEATURE_SECRET_F", "KIONGA_STORAGE_SECRET_S", "KIONGA_PLATFORM_SECRET_P",
             "FEAST_TOKEN", "LAKE_CREDENTIALS", "OPENFAAS_PASSWORD", "MLFLOW_TOKEN", "KFP_TOKEN"]
    path = with_references(tmp_path, {"files": names}, write=names)
    vm.deployment_environment(path)
    gateway = rendered(path)["services"]["gateway"]["environment"]
    for name in names:
        assert gateway[f"{name}_FILE"] == f"/run/secrets/{name}"


def test_every_allowlist_becomes_its_gateway_setting(tmp_path):
    block = {key: ["OLD_" + key.split("_")[0].upper()] for key in vm.REFERENCE_ALLOWLISTS}
    gateway = rendered(with_references(tmp_path, block))["services"]["gateway"]["environment"]
    for key, variable in vm.REFERENCE_ALLOWLISTS.items():
        assert gateway[variable] == block[key][0]


@pytest.mark.parametrize("references,message", [
    ({"files": ["OTHER_TOKEN"]}, "needs a scope prefix"),
    ({"platform_allowlist": ["OPENFAAS_PASSWORD"]}, "cannot name OPENFAAS_PASSWORD"),
    ({"feature_allowlist": ["KIONGA_STORAGE_SECRET_REFS"]}, "cannot name KIONGA_STORAGE_SECRET_REFS"),
    ({"files": ["KIONGA_KB_SECRET_X_FILE"]}, "cannot name KIONGA_KB_SECRET_X_FILE"),
    ({"files": ["DATABASE_URL"], "knowledge_allowlist": ["DATABASE_URL"]}, "cannot name DATABASE_URL"),
    ({"connection_allowlist": ["KIONGA_INTERNAL_TOKEN"]}, "cannot name KIONGA_INTERNAL_TOKEN"),
    ({"connection_allowlist": ["KIONGA_SANDBOX_TOKEN"]}, "cannot name KIONGA_SANDBOX_TOKEN"),
    ({"knowledge_allowlist": ["KIONGA_KB_SECRET_REFS"]}, "cannot name KIONGA_KB_SECRET_REFS"),
    ({"files": ["KIONGA_CONNECTION_SECRET_REFS"]}, "cannot name KIONGA_CONNECTION_SECRET_REFS"),
    ({"knowledge_allowlist": ["S3_SECRET_KEY"]}, "cannot name S3_SECRET_KEY"),
    ({"files": ["bad name"]}, "environment variable names"),
    ({"other": []}, "accepts only"),
])
def test_bad_references_are_refused(tmp_path, references, message):
    path = with_references(tmp_path, references)
    with pytest.raises(ValueError, match=message):
        vm.deployment_environment(path)


def test_a_reference_file_is_checked_like_every_secret(tmp_path):
    name = "KIONGA_KB_SECRET_DOCS"
    path = with_references(tmp_path, {"files": [name]})
    with pytest.raises(ValueError, match=f"missing or empty secret file: {name}"):
        vm.deployment_environment(path)
    data = json.loads(path.read_text())
    secret = Path(data["secrets_directory"]) / name
    secret.write_text("reference-value")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="world-accessible"):
        vm.deployment_environment(path)


def test_refused_names_cover_the_gateway_secret_list():
    config = ROOT.parent / "platform" / "go" / "internal/runtimeconfig/config.go"
    if not config.is_file():
        pytest.skip("platform is not checked out beside this repository")
    names = re.search(r"SecretNames = \[\]string\{(.*?)\}", config.read_text()).group(1)
    assert set(re.findall(r'"([A-Z0-9_]+)"', names)) <= set(vm.GATEWAY_OWN_SECRETS)


def test_reference_rules_match_platform_once_they_exist():
    rules = ROOT.parent / "platform" / "go" / "internal/runtimeconfig/secret_refs.go"
    if not rules.is_file():
        pytest.skip("the reference rules are not in this platform checkout yet")
    text = rules.read_text()
    assert set(re.findall(r'Prefix:\s*"([A-Z_]+)"', text)) | set(re.findall(r'Prefix: "([A-Z_]+)"', text)) == set(vm.REFERENCE_PREFIXES)
    assert set(re.findall(r'Allowlist:\s*"([A-Z_]+)"', text)) == set(vm.REFERENCE_ALLOWLISTS.values())
    names = {name for block in re.findall(r"Names:\s*\[\]string\{(.*?)\}", text) for name in re.findall(r'"([A-Z_]+)"', block)}
    assert names == set(vm.REFERENCE_NAMES)
    assert set(re.findall(r'scope\.Exempt = "([A-Z_]+)"', text)) == set(vm.REFERENCE_EXEMPT)
    assert "loadReferenceFiles" in text


class Calls:
    """Records every command main() runs, and can fail one by matching text."""

    def __init__(self, monkeypatch, fail=""):
        self.commands = []
        self.fail = fail
        monkeypatch.setattr(vm.subprocess, "run", self.run)

    def run(self, command, **kwargs):
        self.commands.append(command)
        failed = bool(self.fail) and self.fail in " ".join(command)
        if failed and kwargs.get("check"):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 1 if failed else 0)

    def index(self, text):
        return next(i for i, command in enumerate(self.commands) if text in " ".join(command))

    def ran(self, text):
        return any(text in " ".join(command) for command in self.commands)


def run_main(monkeypatch, path, *flags):
    monkeypatch.setattr("sys.argv", ["up.py", str(path), *flags])
    monkeypatch.setattr(vm.time, "sleep", lambda _: None)
    monkeypatch.setattr(vm.urllib.request, "urlopen", lambda *a, **k: FakeReady())
    vm.main()


class FakeReady:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_a_real_probe_runs_before_anything_is_pulled_or_started(tmp_path, monkeypatch):
    path, _ = inputs(tmp_path, enabled())
    calls = Calls(monkeypatch)
    run_main(monkeypatch, path)
    probe = calls.index("check-sandbox-runtime.sh")
    assert probe < calls.index(".yaml pull") < calls.index("docker pull registry/sandbox") < calls.index(" up -d")


def test_a_failed_probe_starts_nothing(tmp_path, monkeypatch):
    path, _ = inputs(tmp_path, enabled())
    calls = Calls(monkeypatch, fail="check-sandbox-runtime.sh")
    with pytest.raises(RuntimeError, match="cannot run a container under gVisor"):
        run_main(monkeypatch, path)
    assert not calls.ran(" pull") and not calls.ran(" up -d")


def test_a_failed_sandbox_image_pull_starts_nothing(tmp_path, monkeypatch):
    path, _ = inputs(tmp_path, enabled())
    calls = Calls(monkeypatch, fail="docker pull registry/sandbox")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, path)
    assert not calls.ran(" up -d")


@pytest.mark.parametrize("teams", [None, enabled(sandbox=False)])
def test_no_probe_without_sandboxes(tmp_path, monkeypatch, teams):
    path, _ = inputs(tmp_path, teams)
    calls = Calls(monkeypatch)
    run_main(monkeypatch, path)
    assert not calls.ran("check-sandbox-runtime.sh") and calls.ran(" up -d")


@pytest.mark.parametrize("flag", ["--validate-only", "--stop"])
def test_validate_and_stop_touch_no_runtime(tmp_path, monkeypatch, flag):
    path, _ = inputs(tmp_path, enabled())
    calls = Calls(monkeypatch)
    run_main(monkeypatch, path, flag)
    assert not calls.ran("check-sandbox-runtime.sh") and not calls.ran(" up -d")
    assert calls.ran("compose.sandbox.yaml"), "stop and validate cover the same services"


def test_the_probe_script_is_given_docker_and_no_image_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KIONGA_RUNSC_PROBE_IMAGE", "attacker/image:latest")
    path, _ = inputs(tmp_path, enabled())
    seen = {}

    def run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(vm.subprocess, "run", run)
    vm.check_sandbox_runtime(vm.deployment_environment(path))
    assert seen["env"]["KIONGA_ENGINE"] == "docker"
    assert "KIONGA_RUNSC_PROBE_IMAGE" not in seen["env"]
    assert re.search(r"busybox:1\.37@sha256:[a-f0-9]{64}", (ROOT / "scripts/check-sandbox-runtime.sh").read_text())
    assert os.access(ROOT / "scripts/check-sandbox-runtime.sh", os.X_OK)
