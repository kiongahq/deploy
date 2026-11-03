"""The trace proxy authenticates callers with project keys and holds the provider
key itself (kiongahq/trace-proxy#40). Compose must wire both sides and must not
ship a key anyone could read in this repository."""

import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

from test_local_secrets import STRONG, checkout, run_ensure, stored  # noqa: F401 (checkout is a fixture)
from test_production_deploy import render_chart
from test_sso import PUBLIC_ENV, ROOT, compose_config

KEY_VARS = ("TRACE_PROXY_PROJECT_KEY", "TRACE_PROXY_KEYS")
AGENTS = ("agent-runtime", "agent-designer-runner")

needs_compose = pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose is required for config validation")


def render_without(*missing, files=("compose.yaml",), extra=None):
    """Run `compose config` with variables removed; returns the completed process."""
    env = {k: v for k, v in os.environ.items() if k not in missing}
    env.update({"KIONGA_SRC": str(ROOT.parent), **(extra or {})})
    args = ["docker", "compose", "--env-file", "/dev/null"]
    for name in files:
        args += ["-f", str(ROOT / name)]
    return subprocess.run([*args, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, env=env)


@needs_compose
def test_proxy_gets_hashed_keys_and_the_provider_key():
    env = compose_config("compose.yaml", env={"LLM_UPSTREAM_API_KEY": "sk-provider", "LLM_UPSTREAM_AUTH_HEADER": "x-api-key"})["trace-proxy"]["environment"]
    assert json.loads(env["TRACE_PROXY_KEYS"])[0]["key_sha256"] == "0" * 64
    assert env["LLM_UPSTREAM_API_KEY"] == "sk-provider"
    assert env["LLM_UPSTREAM_AUTH_HEADER"] == "x-api-key"
    assert "TRACE_PROXY_PROJECT_KEY" not in env, "the proxy stores only hashes"
    assert "TRACE_PROXY_ALLOW_UNAUTHENTICATED" not in env


@needs_compose
@pytest.mark.parametrize("agent", AGENTS)
def test_agents_call_the_proxy_with_the_project_key_not_the_provider_key(agent):
    env = compose_config("compose.yaml", env={"OPENAI_API_KEY": "sk-provider", "LLM_UPSTREAM_API_KEY": "sk-provider",
                                              "TRACE_PROXY_PROJECT_KEY": "the-project-key"})[agent]["environment"]
    assert env["OPENAI_API_KEY"] == "the-project-key"
    assert env["MLAIOPS_LLM_BASE_URL"] == "http://trace-proxy:8081/v1"
    assert "sk-provider" not in env.values()


@needs_compose
def test_workspaces_keep_their_own_openai_key():
    services = compose_config("compose.yaml", env={"OPENAI_API_KEY": "sk-workspace"})
    assert services["jupyter"]["environment"]["OPENAI_API_KEY"] == "sk-workspace"


@needs_compose
@pytest.mark.parametrize("name", KEY_VARS)
def test_compose_refuses_to_start_without_keys(name):
    result = render_without(name)
    assert result.returncode != 0
    assert name in result.stderr and ".env.example" in result.stderr


def test_compose_has_no_fixed_key():
    text = (ROOT / "compose.yaml").read_text()
    for name in KEY_VARS:
        assert not re.search(r"\$\{" + name + r":?-", text), f"{name} must not have a default"
    assert "key_sha256" not in text, "no key hash may be committed"
    assert "TRACE_PROXY_ALLOW_UNAUTHENTICATED" not in text


def test_env_example_documents_the_keys_and_commits_none():
    text = (ROOT / ".env.example").read_text()
    for name in (*KEY_VARS, "LLM_UPSTREAM_API_KEY", "LLM_UPSTREAM_AUTH_HEADER", "LLM_UPSTREAM_AUTH_SCHEME"):
        assert f"#{name}=" in text
    assert not re.search(r"^TRACE_PROXY_", text, re.M), "the example must not set a key"
    assert not re.search(r"[0-9a-f]{64}", text), "the example must not contain a key hash"


@needs_compose
def test_env_example_recipe_produces_keys_the_shell_and_compose_both_read(tmp_path):
    # The documented commands, run for real, then read back the way public-up.sh
    # (source) and Compose (--env-file) read .env.
    lines = (ROOT / ".env.example").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "key=$(openssl rand" in line)
    recipe = "\n".join(line.lstrip("#").strip() for line in lines[start:start + 4])
    generated = subprocess.run(["bash", "-c", recipe], capture_output=True, text=True, check=True).stdout
    env_file = tmp_path / ".env"
    env_file.write_text(generated)

    sourced = subprocess.run(["bash", "-c", f"set -a; source {env_file}; printf %s \"$TRACE_PROXY_KEYS\""], capture_output=True, text=True, check=True).stdout
    entry = json.loads(sourced)[0]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["key_sha256"]) and entry["key_id"] and entry["project"]

    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    result = subprocess.run(["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"],
                            cwd=ROOT, capture_output=True, text=True, env={**env, "KIONGA_SRC": str(ROOT.parent)})
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert json.loads(services["trace-proxy"]["environment"]["TRACE_PROXY_KEYS"]) == [entry]
    key = services["agent-runtime"]["environment"]["OPENAI_API_KEY"]
    assert subprocess.run(["openssl", "dgst", "-sha256", "-r"], input=key, capture_output=True, text=True, check=True).stdout.split()[0] == entry["key_sha256"]


@needs_compose
@pytest.mark.parametrize("name", KEY_VARS)
def test_public_mode_requires_operator_keys(name):
    result = render_without(name, files=("compose.yaml", "compose.public.yaml"), extra=PUBLIC_ENV)
    assert result.returncode != 0
    assert name in result.stderr and ".env.example" in result.stderr


@needs_compose
def test_public_mode_passes_operator_keys_through_and_stays_unpublished():
    services = compose_config("compose.yaml", "compose.public.yaml",
                              env={**PUBLIC_ENV, "TRACE_PROXY_PROJECT_KEY": "operator-key", "TRACE_PROXY_KEYS": '[{"key_id":"ops"}]'})
    assert services["trace-proxy"]["environment"]["TRACE_PROXY_KEYS"] == '[{"key_id":"ops"}]'
    assert not services["trace-proxy"].get("ports")
    for agent in AGENTS:
        assert services[agent]["environment"]["OPENAI_API_KEY"] == "operator-key"


def test_public_overlay_has_no_fixed_key():
    text = (ROOT / "compose.public.yaml").read_text()
    for name in KEY_VARS:
        assert not re.search(r"\$\{" + name + r":?-", text), f"{name} must not have a default"
    assert "TRACE_PROXY_ALLOW_UNAUTHENTICATED" not in text


def proxy_container(docs):
    deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "kionga-trace-proxy")
    return deployment["spec"]["template"]["spec"]


def test_chart_mounts_trace_proxy_keys_from_a_secret(tmp_path):
    pod = proxy_container(render_chart(tmp_path))
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["TRACE_PROXY_KEYS_FILE"] == "/run/secrets/TRACE_PROXY_KEYS"
    assert "TRACE_PROXY_KEYS" not in env and "LLM_UPSTREAM_API_KEY_FILE" not in env
    secret = next(v for v in pod["volumes"] if v["name"] == "secrets")["secret"]
    assert secret["secretName"] == "kionga-tracing"
    assert secret["items"] == [{"key": "TRACE_PROXY_KEYS", "path": "TRACE_PROXY_KEYS"}]


def test_chart_mounts_the_provider_key_when_listed(tmp_path):
    pod = proxy_container(render_chart(tmp_path, components={"trace-proxy": {"secrets": ["TRACE_PROXY_KEYS", "LLM_UPSTREAM_API_KEY"]}}))
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["LLM_UPSTREAM_API_KEY_FILE"] == "/run/secrets/LLM_UPSTREAM_API_KEY"
    secret = next(v for v in pod["volumes"] if v["name"] == "secrets")["secret"]
    assert {"key": "LLM_UPSTREAM_API_KEY", "path": "LLM_UPSTREAM_API_KEY"} in secret["items"]


def test_production_example_sources_trace_proxy_keys_externally():
    example = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())
    assert [t["secretKey"] for t in example["externalSecrets"]["targets"]["kionga-tracing"]] == ["TRACE_PROXY_KEYS"]


def generated_keys(path):
    return json.loads(stored(path)["TRACE_PROXY_KEYS"].strip("'"))


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_local_up_generates_a_random_key_and_derives_its_hash(checkout):  # noqa: F811
    assert run_ensure(checkout).returncode == 0
    values = stored(checkout)
    assert re.fullmatch(r"[0-9a-f]{48}", values["TRACE_PROXY_PROJECT_KEY"])
    [entry] = generated_keys(checkout)
    assert entry == {"key_id": "local-agents", "key_sha256": sha256(values["TRACE_PROXY_PROJECT_KEY"]), "project": "local"}
    assert values["TRACE_PROXY_PROJECT_KEY"] not in values["TRACE_PROXY_KEYS"]


def test_two_checkouts_get_different_keys(tmp_path):
    keys = []
    for name in ("one", "two"):
        path = tmp_path / name
        (path / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/local-up.sh", path / "scripts")
        assert run_ensure(path).returncode == 0
        keys.append(stored(path)["TRACE_PROXY_PROJECT_KEY"])
    assert keys[0] != keys[1]


def test_restart_keeps_key_and_rotation_updates_the_hash(checkout):  # noqa: F811
    assert run_ensure(checkout).returncode == 0
    first = stored(checkout)
    assert run_ensure(checkout).returncode == 0
    assert stored(checkout) == first
    (checkout / ".env").write_text(f"TRACE_PROXY_PROJECT_KEY={STRONG}\n")
    assert run_ensure(checkout).returncode == 0
    assert stored(checkout)["TRACE_PROXY_PROJECT_KEY"] == STRONG
    assert generated_keys(checkout)[0]["key_sha256"] == sha256(STRONG)


def test_operator_key_list_is_kept_and_a_mismatch_warns(checkout):  # noqa: F811
    own = f'[{{"key_id":"mine","key_sha256":"{sha256(STRONG)}","project":"p"}}]'
    (checkout / ".env").write_text(f"TRACE_PROXY_PROJECT_KEY={STRONG}\nTRACE_PROXY_KEYS='{own}'\n")
    result = run_ensure(checkout)
    assert result.returncode == 0 and "401" not in result.stderr
    assert generated_keys(checkout)[0]["key_id"] == "mine"
    (checkout / ".local-state/secrets.env").unlink()
    (checkout / ".env").write_text(f"TRACE_PROXY_KEYS='{own}'\n")
    result = run_ensure(checkout)
    assert result.returncode == 0 and "401" in result.stderr
    assert STRONG not in result.stderr


def test_generated_secrets_never_print_the_key(checkout):  # noqa: F811
    result = run_ensure(checkout)
    assert stored(checkout)["TRACE_PROXY_PROJECT_KEY"] not in result.stdout + result.stderr


def test_public_up_never_reads_generated_keys():
    assert "secrets.env" not in (ROOT / "public-up.sh").read_text()


def test_operator_key_list_with_a_single_quote_stops_the_start(checkout):  # noqa: F811
    result = run_ensure(checkout, TRACE_PROXY_KEYS="['oops']")
    assert result.returncode != 0 and "single quote" in result.stderr
    assert not (checkout / ".local-state/secrets.env").exists()


def test_every_ci_job_using_compose_generates_keys_first():
    workflow = yaml.safe_load((ROOT / ".github/workflows/pressure.yml").read_text())
    checked = 0
    for job in workflow["jobs"].values():
        runs = [step.get("run", "") for step in job["steps"]]
        first_compose = next((i for i, run in enumerate(runs) if "docker compose" in run), None)
        if first_compose is None:
            continue
        checked += 1
        generators = [i for i, run in enumerate(runs) if "TRACE_PROXY_PROJECT_KEY=" in run and "GITHUB_ENV" in run]
        assert generators and generators[0] < first_compose
        assert "add-mask" in runs[generators[0]]
        for name in ("SERVING_OBJECTSTORE_ACCESS_KEY", "SERVING_OBJECTSTORE_SECRET_KEY"):
            assert f"{name}=" in runs[generators[0]]
    assert checked >= 2


def test_scripts_reach_compose_through_the_wrapper_that_loads_generated_keys():
    # A direct `docker compose` stops on the unset keys, and a caller that hides
    # stderr would silently skip its work. public-up.sh reads .env instead.
    for script in (ROOT / "scripts").glob("*.sh"):
        if script.name == "container.sh":
            continue
        commands = [line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")]
        assert not any("docker compose" in line for line in commands), f"{script.name} bypasses container.sh"


def test_public_preflight_requires_the_keys_when_it_exists():
    # scripts/public-preflight.sh arrives with the public-mode preflight change;
    # whichever change lands second keeps the two in step.
    preflight = ROOT / "scripts/public-preflight.sh"
    if not preflight.exists():
        pytest.skip("no public preflight on this branch")
    required = next(line for line in preflight.read_text().splitlines() if line.startswith("REQUIRED="))
    overlay = (ROOT / "compose.public.yaml").read_text()  # not YAML-safe: it uses !reset
    service = overlay[overlay.index("public-preflight:"):overlay.index("  caddy:")]
    for name in (*KEY_VARS, "SERVING_OBJECTSTORE_ACCESS_KEY", "SERVING_OBJECTSTORE_SECRET_KEY"):
        assert name in required, f"add {name} to REQUIRED in public-preflight.sh"
        assert "%s: ${%s:?" % (name, name) in service, f"pass {name} to the public-preflight service"
