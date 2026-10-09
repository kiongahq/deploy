"""Guard the small local startup path against removed registry images."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_compose_reuses_existing_images_for_bucket_init_and_materializer():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "deploy/compose.yaml"),
         "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["minio-init"]["image"] == services["mlflow"]["image"]
    assert services["feature-materializer"]["image"] == services["agent-runtime"]["image"]
    assert "minio/mc" not in result.stdout


def test_ide_and_gateway_builds_use_small_contexts():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "deploy/compose.yaml"),
         "--profile", "ide", "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["ide"]["build"]["context"] == str(ROOT / "deploy")
    assert services["ide"]["build"]["args"]["INSTALL_CODING_AGENTS"] == "false"
    assert services["gateway"]["build"]["context"] == str(ROOT / "go")


def test_bucket_bootstrap_is_idempotent(monkeypatch):
    pytest.importorskip("boto3")
    spec = importlib.util.spec_from_file_location("minio_init", ROOT / "deploy/minio-init.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    created = []

    class Client:
        def list_buckets(self):
            return {"Buckets": []}

        def create_bucket(self, *, Bucket):
            created.append(Bucket)
            if Bucket == module.BUCKETS[0]:
                raise module.ClientError(
                    {"Error": {"Code": "BucketAlreadyOwnedByYou", "Message": "exists"}},
                    "CreateBucket",
                )

    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:9000")
    monkeypatch.setattr(module.boto3, "client", lambda *_, **__: Client())
    module.main()
    assert tuple(created) == module.BUCKETS


def _load_build_inputs():
    spec = importlib.util.spec_from_file_location("build_inputs", ROOT / "scripts/build_inputs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jupyter_base_url_matches_gateway_proxy_prefix():
    """The gateway proxies and probes /workspaces/workbench/ unchanged."""
    entrypoint = (ROOT / "deploy/jupyter/entrypoint.sh").read_text()
    workspaces = (ROOT / "go/internal/httpapi/workspaces.go").read_text()
    assert "--ServerApp.base_url=/workspaces/workbench/" in entrypoint
    assert '"/workspaces/workbench/api"' in workspaces
    compose = (ROOT / "deploy/compose.yaml").read_text()
    assert "/workspaces/workbench/api/status" in compose


def test_copy_sources_ignore_stage_copies(tmp_path):
    module = _load_build_inputs()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.11 AS base\n"
        "COPY --chown=dev python/a python/b /opt/\n"
        "COPY --from=base /x /y\n"
        "ADD deploy/entry.sh \\\n    /usr/local/bin/entry\n"
    )
    assert module.copy_sources(dockerfile) == ["python/a", "python/b", "deploy/entry.sh"]


def test_stale_detects_changed_copy_input_and_new_image(tmp_path):
    """Regression: a cached image must not survive an entrypoint change."""
    module = _load_build_inputs()
    (tmp_path / "deploy").mkdir()
    entry = tmp_path / "deploy/entrypoint.sh"
    entry.write_text("jupyter lab\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\nCOPY deploy/entrypoint.sh /bin/entry\n")
    config = {"services": {
        "jupyter": {"image": "kionga-jupyter", "build": {"context": str(tmp_path), "dockerfile": "Dockerfile"}},
        "redis": {"image": "redis:7"},
    }}
    ids = {"kionga-jupyter": "sha256:one"}
    lookup = lambda image: ids.get(image, "")  # noqa: E731

    assert module.stale(config, {}, lookup) == ["jupyter"]
    state = module.record(config, {}, ["jupyter"], lookup)
    assert module.stale(config, state, lookup) == []

    entry.write_text("jupyter lab --ServerApp.base_url=/workspaces/workbench/\n")
    assert module.stale(config, state, lookup) == ["jupyter"]

    state = module.record(config, state, ["jupyter"], lookup)
    ids["kionga-jupyter"] = "sha256:two"
    assert module.stale(config, state, lookup) == ["jupyter"]
    ids.pop("kionga-jupyter")
    assert module.stale(config, state, lookup) == ["jupyter"]


def test_build_inputs_cover_every_compose_build():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    module = _load_build_inputs()
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "deploy/compose.yaml"), "--profile", "ide",
         "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    config = json.loads(result.stdout)
    built = module.built_services(config)
    assert {"gateway", "jupyter", "ide"} <= set(built)
    for name, svc in built.items():
        assert len(module.service_hash(svc["build"])) == 64, name
