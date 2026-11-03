"""The trace proxy authenticates callers with project keys and holds the provider
key itself (kiongahq/trace-proxy#40). Compose must wire both sides and must not
ship a key anyone could read in this repository."""

import json
import os
import re
import shutil
import subprocess

import pytest

from test_sso import PUBLIC_ENV, ROOT, compose_config

KEY_VARS = ("TRACE_PROXY_PROJECT_KEY", "TRACE_PROXY_KEYS")
AGENTS = ("agent-runtime", "agent-designer-runner")

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose is required for config validation")


def render_without(*missing, files=("compose.yaml",), extra=None):
    """Run `compose config` with variables removed; returns the completed process."""
    env = {k: v for k, v in os.environ.items() if k not in missing}
    env.update({"KIONGA_SRC": str(ROOT.parent), **(extra or {})})
    args = ["docker", "compose", "--env-file", "/dev/null"]
    for name in files:
        args += ["-f", str(ROOT / name)]
    return subprocess.run([*args, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, env=env)


def test_proxy_gets_hashed_keys_and_the_provider_key():
    env = compose_config("compose.yaml", env={"LLM_UPSTREAM_API_KEY": "sk-provider", "LLM_UPSTREAM_AUTH_HEADER": "x-api-key"})["trace-proxy"]["environment"]
    assert json.loads(env["TRACE_PROXY_KEYS"])[0]["key_sha256"] == "0" * 64
    assert env["LLM_UPSTREAM_API_KEY"] == "sk-provider"
    assert env["LLM_UPSTREAM_AUTH_HEADER"] == "x-api-key"
    assert "TRACE_PROXY_PROJECT_KEY" not in env, "the proxy stores only hashes"
    assert "TRACE_PROXY_ALLOW_UNAUTHENTICATED" not in env


@pytest.mark.parametrize("agent", AGENTS)
def test_agents_call_the_proxy_with_the_project_key_not_the_provider_key(agent):
    env = compose_config("compose.yaml", env={"OPENAI_API_KEY": "sk-provider", "LLM_UPSTREAM_API_KEY": "sk-provider",
                                              "TRACE_PROXY_PROJECT_KEY": "the-project-key"})[agent]["environment"]
    assert env["OPENAI_API_KEY"] == "the-project-key"
    assert env["MLAIOPS_LLM_BASE_URL"] == "http://trace-proxy:8081/v1"
    assert "sk-provider" not in env.values()


def test_workspaces_keep_their_own_openai_key():
    services = compose_config("compose.yaml", env={"OPENAI_API_KEY": "sk-workspace"})
    assert services["jupyter"]["environment"]["OPENAI_API_KEY"] == "sk-workspace"


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


@pytest.mark.parametrize("name", KEY_VARS)
def test_public_mode_requires_operator_keys(name):
    result = render_without(name, files=("compose.yaml", "compose.public.yaml"), extra=PUBLIC_ENV)
    assert result.returncode != 0
    assert name in result.stderr and ".env.example" in result.stderr


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
