#!/usr/bin/env python3
"""Rotate a running local PostgreSQL role and recreate its active consumers.

Passwords travel through stdin or environment, never command arguments or logs.
The existing database volume is retained. psql's password command hashes the
password before sending ALTER ROLE to PostgreSQL.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ["bash", str(ROOT / "scripts/container.sh")]


def run(args, **kwargs):
    return subprocess.run(ENGINE + args, cwd=ROOT, capture_output=True, text=True, check=True, **kwargs)


def set_password(text, password):
    lines = [line for line in text.splitlines() if not re.match(r"^\s*(?:export\s+)?POSTGRES_PASSWORD\s*=", line)]
    return "\n".join(lines) + "\nPOSTGRES_PASSWORD=" + password + "\n"


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="kionga-postgres-1")
    args = parser.parse_args()
    database = json.loads(run(["inspect", args.container]).stdout)[0]
    if not database["State"]["Running"]:
        raise RuntimeError("Start the existing PostgreSQL container before rotating its password.")
    labels = database["Config"]["Labels"]
    project = labels.get("com.docker.compose.project")
    if not project or labels.get("com.docker.compose.service") != "postgres":
        raise RuntimeError("The target must be the local Compose PostgreSQL service.")
    env = dict(item.split("=", 1) for item in database["Config"]["Env"] if "=" in item)
    user = env.get("POSTGRES_USER", "")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", user):
        raise RuntimeError("Unsupported PostgreSQL role name.")
    ids = run(["ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"]).stdout.split()
    containers = json.loads(run(["inspect", *ids]).stdout)
    consumers = sorted({c["Config"]["Labels"]["com.docker.compose.service"] for c in containers
                        if any(item.startswith(("DATABASE_URL=", "BACKEND_STORE_URI=", "POSTGRES_PASSWORD="))
                               for item in c["Config"].get("Env", []))})
    password = secrets.token_hex(24)
    paths = [ROOT / ".env", ROOT / ".local-state/secrets.env"]
    previous = {path: path.read_text() if path.exists() else None for path in paths}
    try:
        for path in paths:
            write_private(path, set_password(previous[path] or "", password))
        run(["exec", "-i", args.container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user,
             "-d", "postgres", "-c", "\\password " + user], input=password + "\n" + password + "\n")
    except Exception:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write_private(path, content)
        raise RuntimeError("Password rotation did not complete; configuration was restored. Inspect the database before retrying.") from None
    print("Database password rotated and saved privately. Recreating active database consumers.")
    try:
        run(["compose", "-f", str(ROOT / "compose.yaml"), "-p", project,
             "up", "-d", "--no-deps", "--no-build", *consumers], env={**os.environ, "POSTGRES_PASSWORD": password})
    except subprocess.CalledProcessError:
        raise RuntimeError("Password is rotated and saved. Some services could not restart; run make local-start to reconnect them.") from None
    print("Recreated: " + ", ".join(consumers) + ". Database data was preserved.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("Container operation failed. Check the selected engine and running local stack; no credentials were printed.") from None
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
