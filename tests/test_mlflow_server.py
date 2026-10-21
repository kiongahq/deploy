"""The MLflow server migrates its schema before serving, accepts only known
Host headers, and runs the same MLflow release as the images that train and
load models (pipeline-runner, the Jupyter workbench): a model pickled by one
must load in the others."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT.parent


def server_version():
    match = re.search(r"^ARG MLFLOW_VERSION=v(\S+)$", (ROOT / "mlflow" / "Dockerfile").read_text(), re.M)
    assert match, "mlflow/Dockerfile must pin ARG MLFLOW_VERSION=v<version>"
    return match.group(1)


@pytest.fixture(scope="module")
def mlflow():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["mlflow"]


def test_schema_upgrade_runs_before_the_server(mlflow):
    script = " ".join(mlflow["command"])
    assert script.index("mlflow db upgrade") < script.index("exec mlflow server")
    assert "&&" in script  # a failed migration must not start the server


def test_host_header_allowlist_is_explicit(mlflow):
    hosts = mlflow["environment"]["MLFLOW_SERVER_ALLOWED_HOSTS"].split(",")
    assert "*" not in hosts
    assert {"mlflow:5000", "localhost:*"} <= set(hosts)


@pytest.mark.parametrize("path, pattern", [
    ("pipeline-runner/requirements.txt", r"^mlflow==(\S+)$"),
    ("workspace/jupyter/Dockerfile", r'"mlflow==([^"]+)"'),
])
def test_training_images_match_the_server(path, pattern):
    source = SRC / path
    if not source.exists():
        pytest.skip(f"{path} is not checked out beside deploy")
    found = re.findall(pattern, source.read_text(), re.M)
    assert found == [server_version()], f"{path} pins {found}, server runs {server_version()}"
