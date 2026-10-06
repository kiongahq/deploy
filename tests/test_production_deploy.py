import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("vm_deploy", ROOT / "deploy/vm/up.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def vm_inputs(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"MLAIOPS_ALLOWED_ORIGIN": "https://app.company.test"}))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in vm.REQUIRED_SECRETS:
        path = secrets / name
        path.write_text("test-value")
        path.chmod(0o640)
    deployment = dict(domain="app.company.test", acme_email="ops@company.test",
                      gateway_image="registry/gateway@sha256:" + "a" * 64,
                      caddy_image="caddy@sha256:" + "b" * 64,
                      config_file=str(config), secrets_directory=str(secrets))
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(deployment))
    return path, deployment


def test_vm_uses_paths_not_secret_environment(tmp_path, monkeypatch):
    path, data = vm_inputs(tmp_path)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "must-not-inherit")
    env = vm.deployment_environment(path)
    assert "OIDC_CLIENT_SECRET" not in env
    assert "test-value" not in str(env)
    assert env["KIONGA_SECRETS_DIR"] == data["secrets_directory"]


def test_vm_compose_resolves_without_repository_dotenv(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for VM configuration validation")
    path, _ = vm_inputs(tmp_path)
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "deploy/vm/compose.yaml"), "config", "--quiet"],
        env=vm.deployment_environment(path), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("problem", ["tag", "origin", "missing", "world-readable"])
def test_vm_rejects_unsafe_inputs(tmp_path, problem):
    path, data = vm_inputs(tmp_path)
    if problem == "tag":
        data["gateway_image"] = "gateway:latest"
    elif problem == "origin":
        data["domain"] = "other.company.test"
    elif problem == "missing":
        (Path(data["secrets_directory"]) / "DATABASE_URL").unlink()
    else:
        (Path(data["secrets_directory"]) / "DATABASE_URL").chmod(0o644)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        vm.deployment_environment(path)


def helm_binary():
    helm = os.getenv("HELM") or shutil.which("helm")
    local = ROOT / ".tools/helm/darwin-arm64/helm"
    if not helm and local.exists():
        helm = str(local)
    if not helm:
        pytest.skip("Helm required for chart validation; deployment CI installs it")
    return helm


def render_chart(tmp_path):
    values = yaml.safe_load((ROOT / "deploy/helm/production.example.yaml").read_text())
    values["images"] = {name: f"registry/{name}@sha256:" + "a" * 64 for name in values["images"]}
    file = tmp_path / "values.yaml"
    file.write_text(yaml.safe_dump(values))
    result = subprocess.run([helm_binary(), "template", "kionga", str(ROOT / "deploy/helm/kionga"),
                             "--namespace", "kionga-system", "-f", str(file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_managed_chart_security_and_availability(tmp_path):
    docs = render_chart(tmp_path)
    workloads = [d for d in docs if d["kind"] == "Deployment"]
    assert len(workloads) == 6
    assert len([d for d in docs if d["kind"] == "PodDisruptionBudget"]) == 6
    for deployment in workloads:
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert "@sha256:" in container["image"]
        assert container["securityContext"]["readOnlyRootFilesystem"]
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert pod["securityContext"]["runAsNonRoot"]
        assert pod["topologySpreadConstraints"]
    assert not any(d["kind"] in ("ClusterRole", "ClusterRoleBinding", "Secret") for d in docs)
    assert all(d["spec"]["type"] == "ClusterIP" for d in docs if d["kind"] == "Service")
    ingress = next(d for d in docs if d["kind"] == "Ingress")
    assert ingress["spec"]["tls"]
    workspace_ingress = next(d for d in docs if d["kind"] == "Ingress" and d["metadata"]["name"] == "kionga-workspaces")
    assert workspace_ingress["spec"]["rules"][0]["host"] == "*.workspaces.example.com"
    assert workspace_ingress["spec"]["tls"][0]["hosts"] == ["*.workspaces.example.com"]
    assert any(d["kind"] == "ExternalSecret" for d in docs)
    assert len([d for d in docs if d["kind"] == "ServiceMonitor"]) == 3
    assert any(d["kind"] == "PrometheusRule" for d in docs)


def test_chart_refuses_unpinned_default_images():
    result = subprocess.run([helm_binary(), "template", "kionga", str(ROOT / "deploy/helm/kionga"), "--set", "ingress.enabled=false"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "immutable" in result.stderr
