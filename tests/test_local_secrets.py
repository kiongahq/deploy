"""make local-up gives the local stack strong, stable workspace tokens.

The jupyter image refuses the old built-in token and the sidecar refuses a short
job token, so compose.yaml must carry no weak default and every compose call must
render the same generated values. Values are assembled at runtime so secret
scanners never see a literal credential."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess

import pytest
import yaml
from test_sso import ROOT

NAMES = ("JUPYTER_TOKEN", "IDE_PASSWORD", "KIONGA_WORKSPACE_JOB_TOKEN", "KIONGA_SANDBOX_TOKEN", "TRACE_PROXY_PROJECT_KEY",
         "SERVING_OBJECTSTORE_ACCESS_KEY", "SERVING_OBJECTSTORE_SECRET_KEY")
STRONG = "".join(["abcdef0123456789"] * 3)


@pytest.fixture
def checkout(tmp_path):
    """A copy of the two scripts, so ROOT resolves to a scratch checkout."""
    (tmp_path / "scripts").mkdir()
    for name in ("local-up.sh", "container.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    return tmp_path


def run_ensure(checkout, **environment):
    env = {"PATH": os.environ["PATH"], "HOME": str(checkout), **environment}
    return subprocess.run(
        ["bash", "-c", "source scripts/local-up.sh && ensure_local_secrets"],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
    )


def stored(checkout):
    text = (checkout / ".local-state" / "secrets.env").read_text()
    return dict(line.split("=", 1) for line in text.splitlines())


def test_generates_distinct_strong_tokens_in_a_private_file(checkout):
    result = run_ensure(checkout)
    assert result.returncode == 0, result.stderr
    values = stored(checkout)
    assert set(values) == {*NAMES, "TRACE_PROXY_KEYS"}
    assert len({values[name] for name in NAMES}) == len(NAMES)
    assert all(re.fullmatch(r"[0-9a-f]{48}", values[name]) for name in NAMES)
    mode = stat.S_IMODE((checkout / ".local-state" / "secrets.env").stat().st_mode)
    assert mode == 0o600
    assert not [path.name for path in (checkout / ".local-state").iterdir() if path.name != "secrets.env"]


def test_values_are_never_printed(checkout):
    result = run_ensure(checkout, JUPYTER_TOKEN=STRONG)
    for value in stored(checkout).values():
        assert value not in result.stdout + result.stderr


def test_restart_keeps_the_stored_tokens(checkout):
    assert run_ensure(checkout).returncode == 0
    first = stored(checkout)
    assert run_ensure(checkout).returncode == 0
    assert stored(checkout) == first


def test_only_a_missing_token_is_regenerated(checkout):
    assert run_ensure(checkout).returncode == 0
    first = stored(checkout)
    path = checkout / ".local-state" / "secrets.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in first.items() if k != "IDE_PASSWORD"))
    assert run_ensure(checkout).returncode == 0
    second = stored(checkout)
    assert second["JUPYTER_TOKEN"] == first["JUPYTER_TOKEN"]
    assert second["KIONGA_WORKSPACE_JOB_TOKEN"] == first["KIONGA_WORKSPACE_JOB_TOKEN"]
    assert second["IDE_PASSWORD"] != first["IDE_PASSWORD"]


def test_dotenv_value_wins_over_stored(checkout):
    assert run_ensure(checkout).returncode == 0
    (checkout / ".env").write_text(f'JUPYTER_TOKEN="{STRONG}"\n')
    assert run_ensure(checkout).returncode == 0
    assert stored(checkout)["JUPYTER_TOKEN"] == STRONG


def test_weak_dotenv_value_is_ignored_with_a_warning(checkout):
    weak = "-".join(["mlaiops", "local"])
    (checkout / ".env").write_text(f"JUPYTER_TOKEN={weak}\n")
    result = run_ensure(checkout)
    assert result.returncode == 0, result.stderr
    assert "JUPYTER_TOKEN" in result.stderr and weak not in result.stderr
    assert len(stored(checkout)["JUPYTER_TOKEN"]) == 48


def test_weak_shell_value_stops_the_start(checkout):
    result = run_ensure(checkout, JUPYTER_TOKEN="short")
    assert result.returncode != 0
    assert "JUPYTER_TOKEN" in result.stderr and "short" not in result.stderr


def test_container_wrapper_exports_stored_tokens_only_to_compose(checkout):
    assert run_ensure(checkout).returncode == 0
    values = stored(checkout)
    stubs = checkout / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text('#!/bin/sh\necho "args=$1"\nenv\n')
    docker.chmod(0o755)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "KIONGA_ENGINE": "docker"}

    def run(*args, **extra):
        return subprocess.run(["bash", "scripts/container.sh", *args], cwd=checkout, env={**env, **extra},
                              capture_output=True, text=True, timeout=30).stdout

    composed = run("compose", "ps")
    assert all(f"{name}={values[name]}" in composed for name in NAMES)
    assert all(name not in run("ps") for name in NAMES)
    assert f"JUPYTER_TOKEN={STRONG}" in run("compose", "ps", JUPYTER_TOKEN=STRONG)


def test_container_wrapper_keeps_the_json_key_list_intact(checkout):
    # TRACE_PROXY_KEYS holds JSON, which the token pattern would silently drop.
    assert run_ensure(checkout).returncode == 0
    keys = stored(checkout)["TRACE_PROXY_KEYS"]
    stubs = checkout / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text('#!/bin/sh\nprintf %s "$TRACE_PROXY_KEYS"\n')
    docker.chmod(0o755)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "KIONGA_ENGINE": "docker"}
    run = lambda **extra: subprocess.run(["bash", "scripts/container.sh", "compose", "ps"], cwd=checkout, env={**env, **extra},
                                         capture_output=True, text=True, timeout=30).stdout
    assert keys.startswith("'[{") and run() == keys.strip("'")
    assert run(TRACE_PROXY_KEYS="from-shell") == "from-shell"


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose is required for config validation")
def test_compose_through_the_wrapper_renders_generated_values(checkout):
    # The real compose.yaml, rendered from nothing but the generated file, as
    # make local-up and make langflow-up do on every recreation.
    assert run_ensure(checkout).returncode == 0
    values = stored(checkout)
    missing = (*NAMES, "TRACE_PROXY_KEYS")
    env = {k: v for k, v in os.environ.items() if k not in missing}
    env.update({"KIONGA_ENGINE": "docker", "KIONGA_SRC": str(ROOT.parent)})
    result = subprocess.run(["bash", str(checkout / "scripts/container.sh"), "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"],
                            cwd=ROOT, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    key = values["TRACE_PROXY_PROJECT_KEY"]
    [entry] = json.loads(services["trace-proxy"]["environment"]["TRACE_PROXY_KEYS"])
    assert entry["key_sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert services["agent-runtime"]["environment"]["OPENAI_API_KEY"] == key
    assert services["agent-designer-runner"]["environment"]["OPENAI_API_KEY"] == key
    assert services["jupyter"]["environment"]["JUPYTER_TOKEN"] == values["JUPYTER_TOKEN"]
    reader = services["serving-manager"]["environment"]
    assert reader["AWS_ACCESS_KEY_ID"] == values["SERVING_OBJECTSTORE_ACCESS_KEY"]
    assert reader["AWS_SECRET_ACCESS_KEY"] == values["SERVING_OBJECTSTORE_SECRET_KEY"]
    init = services["objectstore-init"]["environment"]
    assert init["SERVING_OBJECTSTORE_SECRET_KEY"] == values["SERVING_OBJECTSTORE_SECRET_KEY"]


def test_state_directory_is_ignored_by_git():
    assert ".local-state/" in (ROOT / ".gitignore").read_text().splitlines()


def test_compose_has_no_weak_workspace_defaults():
    text = (ROOT / "compose.yaml").read_text()
    services = yaml.safe_load(text)["services"]
    for service, variable in [("jupyter", "JUPYTER_TOKEN"), ("gateway", "KIONGA_JUPYTER_TOKEN"), ("ide", "PASSWORD")]:
        assert services[service]["environment"][variable] in ("${JUPYTER_TOKEN:-}", "${IDE_PASSWORD:-}")
    for service in ("jupyter", "gateway", "ide"):
        assert services[service]["environment"]["KIONGA_WORKSPACE_JOB_TOKEN"] == "${KIONGA_WORKSPACE_JOB_TOKEN:-}"
    assert "'mlaiops-local'" not in str(services["jupyter"]["healthcheck"])


def test_local_workbench_mounts_a_prefix_not_whole_buckets():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    assert services["jupyter"]["environment"]["S3_MOUNT_PREFIX"] == "projects/local"
