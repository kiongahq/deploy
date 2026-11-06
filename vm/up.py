#!/usr/bin/env python3
"""Deploy pinned VM images using non-secret JSON and pre-materialized secret files."""
import argparse
import atexit
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REQUIRED_SECRETS = ("DATABASE_URL", "OIDC_CLIENT_SECRET", "MLAIOPS_INTERNAL_TOKEN", "KIONGA_CREDENTIAL_KEY")
TEAM_SECRETS = ("KIONGA_DESIGNER_RUNNER_TOKEN", "TRACE_PROXY_PROJECT_KEY", "TRACE_PROXY_KEYS", "LLM_UPSTREAM_API_KEY")
SANDBOX_SECRETS = ("KIONGA_SANDBOX_TOKEN",)
# Shared bearer tokens: long enough that guessing them is not an option.
MIN_TOKEN_LENGTH = {"KIONGA_DESIGNER_RUNNER_TOKEN": 32, "KIONGA_SANDBOX_TOKEN": 32, "TRACE_PROXY_PROJECT_KEY": 32}
# The gateway refuses development values for its own secrets at startup, but it never
# checks these tokens. Same rule as runtimeconfig.ValidateGateway.
DEVELOPMENT_MARKERS = ("local", "change-me")
TEAM_KEYS = {"enabled", "agent_runtime_image", "trace_proxy_image", "llm_upstream_url", "llm_model",
             "llm_upstream_auth_header", "llm_upstream_auth_scheme", "sandbox", "tuning"}
# Positive whole numbers. config.json cannot carry these: it refuses names with TOKEN.
TEAM_TUNING = {"turn_timeout_seconds": "KIONGA_AGENT_TURN_TIMEOUT_SECONDS",
               "compact_threshold_tokens": "KIONGA_TEAM_COMPACT_THRESHOLD_TOKENS",
               "worker_pool": "KIONGA_TEAM_WORKER_POOL",
               "sandbox_retention_seconds": "KIONGA_TEAM_SANDBOX_RETENTION_SECONDS",
               "trace_proxy_write_timeout_seconds": "TRACE_PROXY_WRITE_TIMEOUT_SECONDS"}
# The rules in platform's runtimeconfig/secret_refs.go; a test compares them.
REFERENCE_ALLOWLISTS = {"knowledge_allowlist": "KIONGA_KB_SECRET_REFS", "connection_allowlist": "KIONGA_CONNECTION_SECRET_REFS",
                        "feature_allowlist": "KIONGA_FEATURE_SECRET_REFS", "storage_allowlist": "KIONGA_STORAGE_SECRET_REFS",
                        "platform_allowlist": "KIONGA_PLATFORM_SECRET_REFS"}
REFERENCE_KEYS = {"files", *REFERENCE_ALLOWLISTS}
REFERENCE_PREFIXES = ("KIONGA_KB_SECRET_", "KIONGA_CONNECTION_SECRET_", "KIONGA_FEATURE_SECRET_",
                      "KIONGA_STORAGE_SECRET_", "KIONGA_PLATFORM_SECRET_")
REFERENCE_NAMES = ("KIONGA_QDRANT_KEY", "KIONGA_KNOWLEDGE_DSN", "KIONGA_AGENT_API_TOKEN", "FEAST_TOKEN", "LAKE_CREDENTIALS")
# Gateway secrets that a platform connection of the matching type may use. They can
# be delivered as files, but never allowlisted.
REFERENCE_EXEMPT = ("OPENFAAS_PASSWORD", "MLFLOW_TOKEN", "KFP_TOKEN")
# Mirrors runtimeconfig.SecretNames in platform; the gateway refuses these too. A test
# compares the two when platform is checked out beside this repository.
GATEWAY_OWN_SECRETS = ("DATABASE_URL", "OIDC_CLIENT_SECRET", "MLAIOPS_INTERNAL_TOKEN", "KIONGA_CREDENTIAL_KEY",
                       "S3_ACCESS_KEY", "S3_SECRET_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "REDIS_URL",
                       "KAFKA_REST_TOKEN", "KFP_TOKEN", "MLFLOW_TOKEN", "OPENFAAS_PASSWORD", "LANGFUSE_SECRET_KEY",
                       "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_UPSTREAM_API_KEY", "KIONGA_JUPYTER_TOKEN",
                       "KIONGA_IDE_PASSWORD", "TRACE_SINK_TOKEN", "MLAIOPS_LOCAL_PASSWORD",
                       "KIONGA_AGENT_DESIGNER_RUNNER_TOKEN", "KIONGA_DESIGNER_RUNNER_TOKEN", "KIONGA_SANDBOX_TOKEN")
# There is deliberately no key for a runtime or the unsafe flag.
SANDBOX_KEYS = {"enabled", "manager_image", "images", "git_hosts"}
DIGEST = r"[^\s]+@sha256:[a-f0-9]{64}"
HOST = r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?"
ROOT = Path(__file__).resolve().parent.parent


def check_secret_file(directory, name):
    secret = directory / name
    if not secret.is_file() or secret.stat().st_size == 0:
        raise ValueError(f"missing or empty secret file: {name}")
    if secret.stat().st_mode & 0o007:
        raise ValueError(f"{name}: secret files must not be world-accessible")
    if not os.access(secret, os.R_OK):
        raise ValueError(f"{name}: deploy user cannot read secret file")
    minimum = MIN_TOKEN_LENGTH.get(name)
    if minimum:
        value = secret.read_bytes().rstrip(b"\r\n")
        if len(value) < minimum:
            raise ValueError(f"{name} must be at least {minimum} characters")
        if any(marker.encode() in value.lower() for marker in DEVELOPMENT_MARKERS):
            raise ValueError(f"{name} looks like a development value; generate a new one")


def pinned(block, label, key):
    if not re.fullmatch(DIGEST, str(block.get(key, ""))):
        raise ValueError(f"{label}.{key} must be pinned to a verified SHA256 image digest")
    return block[key]


def team_settings(data):
    """Validated agent_teams block as (teams, sandbox), each empty when off."""
    teams = data.get("agent_teams", {})
    if not isinstance(teams, dict):
        raise ValueError("agent_teams must be an object")
    sandbox = teams.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("agent_teams.sandbox must be an object")
    for label, block, known in (("agent_teams", teams, TEAM_KEYS), ("agent_teams.sandbox", sandbox, SANDBOX_KEYS)):
        unknown = set(block) - known
        if unknown:
            raise ValueError(f"{label} has unknown settings: {', '.join(sorted(unknown))}")
        if block.get("enabled", False) not in (True, False):
            raise ValueError(f"{label}.enabled must be true or false")
    if not teams.get("enabled"):
        if sandbox.get("enabled"):
            raise ValueError("agent_teams.sandbox.enabled needs agent_teams.enabled")
        return {}, {}
    pinned(teams, "agent_teams", "agent_runtime_image")
    pinned(teams, "agent_teams", "trace_proxy_image")
    upstream = urllib.parse.urlparse(str(teams.get("llm_upstream_url", "")))
    if upstream.scheme not in ("http", "https") or not upstream.hostname or upstream.username or upstream.password:
        raise ValueError("agent_teams.llm_upstream_url must be an http(s) URL without credentials")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", str(teams.get("llm_model", ""))):
        raise ValueError("agent_teams.llm_model is required")
    tuning = teams.get("tuning", {})
    if not isinstance(tuning, dict) or set(tuning) - set(TEAM_TUNING) - {"scheduler"}:
        raise ValueError(f"agent_teams.tuning accepts only: {', '.join(sorted({*TEAM_TUNING, 'scheduler'}))}")
    for key in TEAM_TUNING:
        value = tuning.get(key, 1)
        if type(value) is not int or value < 1:
            raise ValueError(f"agent_teams.tuning.{key} must be a positive whole number")
    if tuning.get("trace_proxy_write_timeout_seconds", 1) > 3600:
        raise ValueError("agent_teams.tuning.trace_proxy_write_timeout_seconds must be from 1 to 3600")
    if tuning.get("scheduler", "off") != "off":
        raise ValueError("agent_teams.tuning.scheduler can only be off")
    if not sandbox.get("enabled"):
        return teams, {}
    pinned(sandbox, "agent_teams.sandbox", "manager_image")
    images = sandbox.get("images")
    if not isinstance(images, list) or not images or not all(re.fullmatch(DIGEST, str(image)) for image in images):
        raise ValueError("agent_teams.sandbox.images must list images pinned to a verified SHA256 digest")
    hosts = sandbox.get("git_hosts", [])
    if not isinstance(hosts, list) or not all(re.fullmatch(HOST, str(host)) for host in hosts):
        raise ValueError("agent_teams.sandbox.git_hosts must list DNS hostnames")
    return teams, sandbox


def deployment_environment(path):
    data = json.loads(Path(path).read_text())
    for name in ("gateway_image", "caddy_image"):
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", data.get(name, "")):
            raise ValueError(f"{name} must be pinned to a verified SHA256 image digest")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", data.get("domain", "")):
        raise ValueError("domain must be a DNS hostname, without a scheme or port")
    if data["domain"].endswith(".example.com") or data["domain"] == "example.com":
        raise ValueError("replace the example domain")
    if not re.fullmatch(r"[^\s@{}]+@[^\s@{}]+", data.get("acme_email", "")):
        raise ValueError("acme_email is required")
    config = Path(data["config_file"]).resolve(strict=True)
    values = json.loads(config.read_text())
    if values.get("MLAIOPS_ALLOWED_ORIGIN") != "https://" + data["domain"]:
        raise ValueError("config public origin must match the deployment domain")
    if any(key.startswith("KIONGA_SANDBOX_") for key in values):
        raise ValueError("sandbox settings belong in agent_teams.sandbox in the deployment file, not the config file")
    teams, sandbox = team_settings(data)
    directory = Path(data["secrets_directory"]).resolve(strict=True)
    references = reference_settings(data)[0]
    for name in (*REQUIRED_SECRETS, *(TEAM_SECRETS if teams else ()), *(SANDBOX_SECRETS if sandbox else ()), *references):
        check_secret_file(directory, name)
    # Do not allow repository .env or inherited Kionga variables to change a release.
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "SSH_AUTH_SOCK",
        "SSL_CERT_FILE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")}
    env.update(KIONGA_DOMAIN=data["domain"], KIONGA_ACME_EMAIL=data["acme_email"],
               KIONGA_GATEWAY_IMAGE=data["gateway_image"], KIONGA_CADDY_IMAGE=data["caddy_image"],
               KIONGA_CONFIG_PATH=str(config), KIONGA_SECRETS_DIR=str(directory))
    if teams:
        env.update(KIONGA_AGENT_RUNTIME_IMAGE=teams["agent_runtime_image"], KIONGA_TRACE_PROXY_IMAGE=teams["trace_proxy_image"],
                   KIONGA_LLM_UPSTREAM_URL=teams["llm_upstream_url"], KIONGA_LLM_MODEL=teams["llm_model"],
                   KIONGA_LLM_UPSTREAM_AUTH_HEADER=teams.get("llm_upstream_auth_header", ""),
                   KIONGA_LLM_UPSTREAM_AUTH_SCHEME=teams.get("llm_upstream_auth_scheme", ""))
        tuning = teams.get("tuning", {})
        env.update({variable: str(tuning[key]) for key, variable in TEAM_TUNING.items() if key in tuning})
        if "scheduler" in tuning:
            env["KIONGA_TEAM_SCHEDULER"] = tuning["scheduler"]
    if sandbox:
        env.update(KIONGA_SANDBOX_MANAGER_IMAGE=sandbox["manager_image"], KIONGA_SANDBOX_IMAGES=",".join(sandbox["images"]),
                   KIONGA_SANDBOX_GIT_HOSTS=",".join(sandbox.get("git_hosts", [])))
    return env


def compose_command(deployment):
    """The Compose command for this deployment. With everything optional off, only the base file."""
    data = json.loads(Path(deployment).read_text())
    teams, sandbox = team_settings(data)
    here = Path(__file__).parent
    files = ["compose.yaml", *(["compose.teams.yaml"] if teams else []), *(["compose.sandbox.yaml"] if sandbox else []),
             *(["compose.sandbox-egress.yaml"] if sandbox.get("git_hosts") else [])]
    command = ["docker", "compose", "--env-file", "/dev/null"]
    for name in files:
        command += ["-f", str(here / name)]
    overlay = reference_overlay(data)
    if overlay:
        command += ["-f", overlay]
    return command


def reference_settings(data):
    """Validated secret_references block as (files, {allowlist setting: names})."""
    block = data.get("secret_references", {})
    if not isinstance(block, dict) or set(block) - REFERENCE_KEYS:
        raise ValueError(f"secret_references accepts only: {', '.join(sorted(REFERENCE_KEYS))}")
    own = {*GATEWAY_OWN_SECRETS, *TEAM_SECRETS, *REFERENCE_ALLOWLISTS.values()}
    files = block.get("files", [])
    allowlists = {key: block.get(key, []) for key in REFERENCE_ALLOWLISTS}
    for key, names in (("files", files), *allowlists.items()):
        if not isinstance(names, list) or not all(re.fullmatch(r"[A-Z][A-Z0-9_]*", str(name)) for name in names):
            raise ValueError("secret_references entries must be environment variable names")
        refused = own & set(names) - (set(REFERENCE_EXEMPT) if key == "files" else set())
        refused |= {name for name in names if name.endswith("_FILE")}
        if refused:
            raise ValueError(f"secret_references cannot name {', '.join(sorted(refused))}")
    listed = {name for names in allowlists.values() for name in names}
    for name in files:
        if not (name.startswith(REFERENCE_PREFIXES) or name in REFERENCE_NAMES or name in REFERENCE_EXEMPT or name in listed):
            raise ValueError(f"secret_references.files: {name} needs a scope prefix such as "
                             "KIONGA_KB_SECRET_, or an entry in an allowlist")
    return files, allowlists


def reference_overlay(data):
    """Write a Compose file that gives the gateway these secret files, and return its path.
    The names are only known here, and the file holds names and paths, never a value."""
    files, allowlists = reference_settings(data)
    if not (files or any(allowlists.values())):
        return None
    directory = Path(data["secrets_directory"]).resolve(strict=True)
    environment = {f"{name}_FILE": f"/run/secrets/{name}" for name in files}
    for key, names in allowlists.items():
        if names:
            environment[REFERENCE_ALLOWLISTS[key]] = ",".join(names)
    overlay = {"services": {"gateway": {"environment": environment}}}
    if files:
        overlay["services"]["gateway"]["secrets"] = list(files)
        overlay["secrets"] = {name: {"file": str(directory / name)} for name in files}
    handle, path = tempfile.mkstemp(prefix="kionga-secret-references-", suffix=".json")
    with os.fdopen(handle, "w") as output:
        json.dump(overlay, output)  # JSON is valid YAML
    atexit.register(lambda: Path(path).unlink(missing_ok=True))
    return path


def check_sandbox_runtime(env):
    """Start a real container under gVisor; an engine's runtime list proves nothing."""
    result = subprocess.run(["bash", str(ROOT / "scripts/check-sandbox-runtime.sh")], env={**env, "KIONGA_ENGINE": "docker"})
    if result.returncode != 0:
        raise RuntimeError("Sandboxes are enabled, but this host cannot run a container under gVisor. Nothing was started.")


def pull_sandbox_images(env):
    # The manager has no registry login, so private images must already be here.
    for image in filter(None, env.get("KIONGA_SANDBOX_IMAGES", "").split(",")):
        if subprocess.run(["docker", "pull", image], env=env).returncode != 0:
            raise SystemExit(PULL_HELP)


PULL_HELP = (
    "Could not pull the pinned images. Kionga's ghcr.io/kiongahq images are private: as the user "
    "running this script, log in once with a GitHub token that has read:packages:\n"
    "  echo \"$GHCR_TOKEN\" | docker login ghcr.io -u <github-user> --password-stdin\n"
    "Docker keeps the credential in ~/.docker/config.json (or $DOCKER_CONFIG). Also check the "
    "digests and the VM's network access to the registry."
)


def pull_images(compose, env):
    result = subprocess.run([*compose, "pull"], env=env)
    if result.returncode != 0:
        raise SystemExit(PULL_HELP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", help="non-secret deployment JSON")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--stop", action="store_true", help="stop services without deleting data")
    args = parser.parse_args()
    env = deployment_environment(args.deployment)
    compose = compose_command(args.deployment)
    subprocess.run([*compose, "config", "--quiet"], env=env, check=True)
    if args.stop:
        subprocess.run([*compose, "stop"], env=env, check=True)
        return
    if args.validate_only:
        print("VM deployment inputs and Compose configuration validated")
        return
    if env.get("KIONGA_SANDBOX_IMAGES"):
        check_sandbox_runtime(env)
    pull_images(compose, env)
    pull_sandbox_images(env)
    subprocess.run([*compose, "up", "-d", "--no-build", "--pull", "never", "--force-recreate"], env=env, check=True)
    url = "https://" + env["KIONGA_DOMAIN"] + "/api/v1/ready"
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    print("Kionga is ready at https://" + env["KIONGA_DOMAIN"])
                    return
        except OSError:
            pass
        time.sleep(3)
    raise RuntimeError("Readiness failed; check DNS, TLS, secrets, database and container logs. No automatic data rollback was attempted.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        sys.exit(1)
