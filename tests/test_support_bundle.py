"""scripts/support-bundle.py: redaction removes every known credential shape,
the collector never reads Secrets, follows Kionga's namespaces from the release
values, and writes a tarball with a manifest of what failed."""

import importlib.util
import json
import subprocess
import sys
import tarfile

import pytest

from test_production_deploy import ROOT

spec = importlib.util.spec_from_file_location("support_bundle", ROOT / "scripts/support-bundle.py")
bundle = importlib.util.module_from_spec(spec)
sys.modules["support_bundle"] = bundle
spec.loader.exec_module(bundle)
R = bundle.REDACTED


@pytest.mark.parametrize("text,secret", [
    ("DATABASE_URL=postgres://kionga:s3cr3t-pass@db:5432/kionga?sslmode=verify-full", "s3cr3t-pass"),
    ("connecting to redis://default:hunter22@redis:6379/0", "hunter22"),
    ("password: hunter2-yaml", "hunter2-yaml"),
    ('{"client_secret": "oidc-abc-123", "level": "info"}', "oidc-abc-123"),
    ("OIDC_CLIENT_SECRET=very-secret-value", "very-secret-value"),
    ("KIONGA_INTERNAL_TOKEN=0123456789abcdef0123", "0123456789abcdef0123"),
    ("Authorization: Bearer abc.def.ghi-token-value", "abc.def.ghi-token-value"),
    ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("cookie: kionga_session=f00dfacecafe", "f00dfacecafe"),
    ("got token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtYXJrIn0.c2lnbmF0dXJlLXZhbHVl from idp", "eyJhbGciOiJSUzI1NiJ9"),
    ("api token nxs_live_abcdefghijklmnop rejected", "nxs_live_abcdefghijklmnop"),
    ("aws key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("push with ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
    ("OPENAI_API_KEY: sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----", "MIIEvQIBADANBg"),
    ("'apiKey': 'quoted-value-1'", "quoted-value-1"),
])
def test_known_credential_shapes_are_redacted(text, secret):
    out = bundle.redact(text)
    assert secret not in out and R in out, out


@pytest.mark.parametrize("text", [
    "level=info msg=\"reconciled KiongaApplication billing\" phase=Ready replicas=3",
    "secretName: kionga-gateway",
    "KIONGA_INTERNAL_TOKEN_FILE=/run/secrets/KIONGA_INTERNAL_TOKEN",
    "valueFrom: {secretKeyRef: {name: tenant-a, key: credentials}}",
    "tokenExpirySeconds: 3600",
    "https://billing.apps.example.com/healthz returned 200",
])
def test_diagnostic_text_survives(text):
    assert bundle.redact(text) == text


def test_redaction_keeps_the_url_shape():
    assert bundle.redact("postgres://kionga:pw@db:5432/x") == f"postgres://kionga:{R}@db:5432/x"


class FakeCluster:
    def __init__(self, values=None, fail=()):
        self.calls, self.values, self.fail = [], values or {}, fail
        self.pods = {"items": [{
            "metadata": {"name": "kionga-gateway-1"},
            "spec": {"initContainers": [{"name": "wait"}], "containers": [{"name": "gateway"}]},
            "status": {"containerStatuses": [{"name": "gateway", "restartCount": 2}],
                       "initContainerStatuses": [{"name": "wait", "restartCount": 0}]}}]}

    def __call__(self, argv):
        self.calls.append(argv)
        joined = " ".join(argv)
        if any(f in joined for f in self.fail):
            return subprocess.CompletedProcess(argv, 1, "", "error: token=leaked-in-stderr forbidden")
        if argv[:1] == ["helm"] and "values" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.values), "")
        if "pods" in argv and "json" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pods), "")
        if "logs" in argv:
            return subprocess.CompletedProcess(argv, 0, "2026-11-19T10:00:00Z msg=start DATABASE_URL=postgres://u:pw1@db/k\n", "")
        return subprocess.CompletedProcess(argv, 0, f"output of {joined}\n", "")


def test_collector_follows_kionga_namespaces_and_never_reads_secrets():
    fake = FakeCluster(values={"workspaceNamespace": "tenant-workloads", "pipelineExecutor": {"kubernetes": True, "namespace": "runs"},
                               "applications": {"enabled": True, "registry": {"url": "r.example.com"}}})
    result = bundle.Collector("kionga", "kionga-system", context="prod", run=fake).collect()
    for argv in fake.calls:
        kinds = [token for arg in argv for token in arg.lower().split(",")]
        assert "secrets" not in kinds and "secret" not in kinds, argv
        assert ("--context" in argv and "prod" in argv) or ("--kube-context" in argv) or argv[:2] == ["helm", "version"]
    for namespace in ("kionga-system", "tenant-workloads", "runs", "kionga-builds"):
        assert f"namespaces/{namespace}/events.txt" in result.files
        assert f"namespaces/{namespace}/kionga-resources.yaml" in result.files
    logs = result.files["namespaces/kionga-system/logs/kionga-gateway-1/gateway.log"]
    assert "pw1" not in logs and R in logs
    assert "namespaces/kionga-system/logs/kionga-gateway-1/gateway.previous.log" in result.files
    assert "namespaces/kionga-system/logs/kionga-gateway-1/wait.previous.log" not in result.files
    assert "prerequisites/keda/pods.txt" in result.files


def test_kubectl_refuses_secrets_even_if_asked():
    collector = bundle.Collector("kionga", "kionga-system", run=FakeCluster())
    for args in (("get", "secrets"), ("get", "pods,secrets"), ("get", "Secret", "x"), ("get", "secrets.v1")):
        with pytest.raises(ValueError):
            collector.kubectl(*args)
    assert collector.kubectl("logs", "secret-rotator-1")[-1] == "secret-rotator-1"


def test_failures_are_recorded_redacted_and_do_not_stop_collection(tmp_path):
    fake = FakeCluster(fail=("history",))
    result = bundle.Collector("kionga", "kionga-system", run=fake).collect()
    [failure] = result.failures
    assert failure["file"] == "helm/history.txt" and "leaked-in-stderr" not in failure["error"]
    assert "helm/status.txt" in result.files


def test_tarball_has_a_manifest_and_private_mode(tmp_path):
    result = bundle.Collector("kionga", "kionga-system", run=FakeCluster(fail=("nodes",))).collect()
    output = bundle.write_tarball(result, tmp_path / "b.tar.gz", {"release": "kionga", "collected_at_epoch": 1795000000.0})
    assert oct(output.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(output) as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("kionga-support/manifest.json"))
    assert "kionga-support/helm/status.txt" in names
    assert manifest["failures"][0]["file"] == "cluster/nodes.txt"
    assert "helm/status.txt" in manifest["files"]


def test_cli_rejects_a_bad_log_window():
    with pytest.raises(SystemExit):
        bundle.main(["--since", "yesterday"])
