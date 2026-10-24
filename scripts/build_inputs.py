#!/usr/bin/env python3
"""Detect Compose services whose local image is older than its build inputs.

`local-up.sh` reuses cached images so offline restarts stay fast. Without this
check a cached image silently survives source changes: a stale Jupyter image
once kept serving at `/` after the entrypoint moved it to
`/workspaces/workbench/`, so every gateway readiness probe returned 404.

For each service with a `build:` section the script hashes the Dockerfile, the
build args, and every file the Dockerfile COPY/ADDs from the context. The hash
is recorded next to the image ID after a build. A service is stale when the
hash or the image ID no longer matches.

Usage:
  compose config --format json | build_inputs.py stale  --state FILE
  compose config --format json | build_inputs.py record --state FILE SERVICE...
  compose config --format json | build_inputs.py registry-images
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "build"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".log")


def copy_sources(dockerfile: Path) -> list[str]:
    """Return context-relative COPY/ADD sources, ignoring --from stages."""
    sources: list[str] = []
    logical = ""
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            logical += line[:-1] + " "
            continue
        logical += line
        parts = shlex.split(logical, comments=True) if logical else []
        logical = ""
        if not parts or parts[0].upper() not in {"COPY", "ADD"}:
            continue
        args = parts[1:]
        if any(a.startswith("--from") for a in args):
            continue
        args = [a for a in args if not a.startswith("--")]
        if len(args) >= 2:
            sources.extend(args[:-1])
    return sources


def iter_files(path: Path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(SKIP_SUFFIXES):
                yield Path(root) / name


def service_hash(build: dict) -> str:
    context = Path(build["context"])
    dockerfile = context / build.get("dockerfile", "Dockerfile")
    digest = hashlib.sha256()
    digest.update(dockerfile.read_bytes())
    digest.update(json.dumps(build.get("args") or {}, sort_keys=True).encode())
    for source in sorted(set(copy_sources(dockerfile))):
        for file in iter_files((context / source).resolve()):
            digest.update(str(file.relative_to(context.resolve())).encode())
            digest.update(file.read_bytes())
    # Named contexts (COPY --from=<name>) are other repositories, e.g. the SDK
    # checkout an agent-runtime image installs. Hash each one whole so a change
    # there marks the image stale too.
    for name, path in sorted((build.get("additional_contexts") or {}).items()):
        root = Path(path).resolve()
        digest.update(f"context:{name}".encode())
        for file in iter_files(root):
            digest.update(str(file.relative_to(root)).encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def image_id(image: str) -> str:
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("container.sh")), "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def built_services(config: dict) -> dict[str, dict]:
    return {name: svc for name, svc in config["services"].items() if svc.get("build")}


def registry_images(config: dict) -> list[str]:
    """Images to pull: every image a service names that no service builds.

    A service may reuse another service's local build by naming its image (the
    feature materializer runs the agent-runtime image); that image must be built,
    never pulled.
    """
    built = {svc.get("image") for svc in built_services(config).values() if svc.get("image")}
    named = {svc["image"] for svc in config["services"].values() if svc.get("image")}
    return sorted(named - built)


def image_name(config: dict, service: str) -> str:
    """The image a built service produces: its `image:`, else Compose's default
    `<project>-<service>` (an empty name would never match, so the service
    would be rebuilt on every start)."""
    svc = config["services"][service]
    return svc.get("image") or f"{config.get('name', '')}-{service}"


def stale(config: dict, state: dict, lookup=image_id) -> list[str]:
    out = []
    for name, svc in sorted(built_services(config).items()):
        recorded = state.get(name) or {}
        current_id = lookup(image_name(config, name))
        if not current_id or recorded.get("image_id") != current_id \
                or recorded.get("inputs") != service_hash(svc["build"]):
            out.append(name)
    return out


def record(config: dict, state: dict, services: list[str], lookup=image_id) -> dict:
    services_cfg = built_services(config)
    for name in services:
        svc = services_cfg[name]
        state[name] = {"inputs": service_hash(svc["build"]), "image_id": lookup(image_name(config, name))}
    return state


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["stale", "record", "registry-images"])
    parser.add_argument("services", nargs="*")
    parser.add_argument("--state", type=Path)
    # The shell passes `record --state FILE SERVICE...`; plain parse_args
    # rejects the trailing services after an option with a nargs="*" positional.
    args = parser.parse_intermixed_args(argv)
    config = json.load(sys.stdin)
    if args.command == "registry-images":
        print("\n".join(registry_images(config)))
        return 0
    if args.state is None:
        parser.error("--state is required for stale and record")
    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    if args.command == "stale":
        print(" ".join(stale(config, state)))
        return 0
    targets = args.services or sorted(built_services(config))
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(record(config, state, targets), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
