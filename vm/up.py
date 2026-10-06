#!/usr/bin/env python3
"""Deploy pinned VM images using non-secret JSON and pre-materialized secret files."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

REQUIRED_SECRETS = ("DATABASE_URL", "OIDC_CLIENT_SECRET", "MLAIOPS_INTERNAL_TOKEN", "KIONGA_CREDENTIAL_KEY")


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
    directory = Path(data["secrets_directory"]).resolve(strict=True)
    for name in REQUIRED_SECRETS:
        secret = directory / name
        if not secret.is_file() or secret.stat().st_size == 0:
            raise ValueError(f"missing or empty secret file: {name}")
        if secret.stat().st_mode & 0o007:
            raise ValueError(f"{name}: secret files must not be world-accessible")
        if not os.access(secret, os.R_OK):
            raise ValueError(f"{name}: deploy user cannot read secret file")
    # Do not allow repository .env or inherited Kionga variables to change a release.
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "SSH_AUTH_SOCK",
        "SSL_CERT_FILE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")}
    env.update(KIONGA_DOMAIN=data["domain"], KIONGA_ACME_EMAIL=data["acme_email"],
               KIONGA_GATEWAY_IMAGE=data["gateway_image"], KIONGA_CADDY_IMAGE=data["caddy_image"],
               KIONGA_CONFIG_PATH=str(config), KIONGA_SECRETS_DIR=str(directory))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", help="non-secret deployment JSON")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--stop", action="store_true", help="stop services without deleting data")
    args = parser.parse_args()
    env = deployment_environment(args.deployment)
    compose = ["docker", "compose", "--env-file", "/dev/null", "-f",
               str(Path(__file__).with_name("compose.yaml"))]
    subprocess.run([*compose, "config", "--quiet"], env=env, check=True)
    if args.stop:
        subprocess.run([*compose, "stop"], env=env, check=True)
        return
    if args.validate_only:
        print("VM deployment inputs and Compose configuration validated")
        return
    subprocess.run([*compose, "pull"], env=env, check=True)
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
