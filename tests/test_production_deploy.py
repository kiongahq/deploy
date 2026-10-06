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
cloud_spec = importlib.util.spec_from_file_location("cloud_preflight", ROOT / "scripts/cloud-preflight.py")
cloud = importlib.util.module_from_spec(cloud_spec)
cloud_spec.loader.exec_module(cloud)


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


@pytest.mark.parametrize("provider,provisioner", [
    ("eks", "ebs.csi.aws.com"), ("gke", "pd.csi.storage.gke.io"),
    ("aks", "disk.csi.azure.com"),
])
def test_provider_overlay_and_storage_class(provider, provisioner, tmp_path):
    overlay = yaml.safe_load((ROOT / f"deploy/helm/providers/{provider}.example.yaml").read_text())
    storage = yaml.safe_load((ROOT / f"deploy/helm/providers/{provider}-storageclass.example.yaml").read_text())
    assert overlay["storageClass"] == storage["metadata"]["name"]
    assert storage["provisioner"] == provisioner
    assert storage["allowVolumeExpansion"] is True
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["reclaimPolicy"] == "Retain"


def test_eks_preflight_fails_without_network_policy(monkeypatch):
    class Args:
        cluster = "staging"
        region = "us-east-1"
        context = "staging"

    def fake_command(*argv):
        if "describe-cluster" in argv:
            return {"cluster": {"status": "ACTIVE", "arn": "arn:aws:eks:us-east-1:123:cluster/staging", "endpoint": "https://eks.example", "identity": {"oidc": {"issuer": "https://oidc.example"}}}}
        if "list-open-id-connect-providers" in argv:
            return {"OpenIDConnectProviderList": [{"Arn": "arn:aws:iam::123:oidc-provider/oidc.example"}]}
        return {"addon": {"status": "ACTIVE", "configurationValues": "{}"}}

    monkeypatch.setattr(cloud, "command", fake_command)
    monkeypatch.setattr(cloud, "check_context_endpoint", lambda *_: None)
    with pytest.raises(ValueError, match="NetworkPolicy is disabled"):
        cloud.check_eks(Args())


def test_cloud_common_rejects_single_zone(monkeypatch):
    def fake_command(*argv):
        if "nodes" in argv:
            return {"items": [{"metadata": {"labels": {"topology.kubernetes.io/zone": "a"}},
                               "status": {"conditions": [{"type": "Ready", "status": "True"}]}}] * 3}
        raise AssertionError("StorageClass must not be reached")

    monkeypatch.setattr(cloud, "command", fake_command)
    with pytest.raises(ValueError, match="two zones"):
        cloud.check_common("staging", "storage", "csi.example")


@pytest.mark.parametrize("provider,payload", [
    ("gke", {"status": "RUNNING", "endpoint": "10.0.0.1",
             "workloadIdentityConfig": {"workloadPool": "project.svc.id.goog"},
             "networkConfig": {"datapathProvider": "ADVANCED_DATAPATH"},
             "addonsConfig": {"gcePersistentDiskCsiDriverConfig": {"enabled": True}}}),
    ("aks", {"provisioningState": "Succeeded", "fqdn": "aks.example",
             "oidcIssuerProfile": {"enabled": True},
             "securityProfile": {"workloadIdentity": {"enabled": True}},
             "networkProfile": {"networkPolicy": "cilium"}}),
])
def test_other_provider_preflights(provider, payload, monkeypatch):
    class Args:
        cluster = "staging"
        project = "project"
        location = "region"
        resource_group = "group"
        context = "staging"

    monkeypatch.setattr(cloud, "command", lambda *_: payload)
    monkeypatch.setattr(cloud, "check_context_endpoint", lambda *_: None)
    getattr(cloud, f"check_{provider}")(Args())


def test_cloud_context_rejects_different_cluster(monkeypatch):
    class Result:
        stdout = "https://unexpected.example"

    monkeypatch.setattr(cloud.subprocess, "run", lambda *_, **__: Result())
    with pytest.raises(ValueError, match="different cluster"):
        cloud.check_context_endpoint("staging", "https://expected.example")
