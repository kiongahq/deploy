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
