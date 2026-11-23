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


def synthetic(*parts):
    """Join parts at run time, so the source holds no credential-shaped literal
    for the repository's secret scanner to flag. None of these is a credential."""
    return "".join(parts)


JWT = synthetic("ey", "JhbGciOiJSUzI1NiJ9", ".", "ey", "JzdWIiOiJtYXJrIn0", ".", "c2lnbmF0dXJlLXZhbHVl")
CASES = [
    (synthetic("DATABASE_URL=postgres://kionga:", "fake-pass", "@db:5432/kionga?sslmode=verify-full"), "fake-pass"),
    (synthetic("connecting to redis://default:", "fake22", "@redis:6379/0"), "fake22"),
    (synthetic("pass", "word: fake-yaml"), "fake-yaml"),
    (synthetic('{"client_', 'secret": "fake-oidc-1", "level": "info"}'), "fake-oidc-1"),
    (synthetic("OIDC_CLIENT_", "SECRET=fake-value-x"), "fake-value-x"),
    (synthetic("KIONGA_INTERNAL_", "TOKEN=", "f" * 20), "f" * 20),
    (synthetic("Authorization: Bearer ", "fake.bearer.value"), "fake.bearer.value"),
    (synthetic("authorization=Basic ", "ZmFrZTpmYWtl"), "ZmFrZTpmYWtl"),
    (synthetic("cookie: kionga_session=", "fakesession1"), "fakesession1"),
    (synthetic("got token ", JWT, " from idp"), JWT[:12]),
    (synthetic("api token nxs_", "fake", "x" * 12, " rejected"), synthetic("nxs_", "fake", "x" * 12)),
    (synthetic("aws key ", "AK", "IA", "Z" * 16), synthetic("AK", "IA", "Z" * 16)),
    (synthetic("AWS_SECRET_ACCESS_", "KEY=", "fake/" * 8), "fake/fake"),
    (synthetic("push with gh", "p_", "z" * 36), synthetic("gh", "p_", "z" * 36)),
    (synthetic("OPENAI_API_", "KEY: sk", "-", "z" * 24), synthetic("sk", "-", "z" * 24)),
    (synthetic("-----BEGIN ", "PRIVATE KEY-----\nZmFrZWtleQ\n-----END ", "PRIVATE KEY-----"), "ZmFrZWtleQ"),
    (synthetic("'api", "Key': 'fake-quoted-1'"), "fake-quoted-1"),
]


@pytest.mark.parametrize("text,secret", CASES)
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


def test_literal_env_values_are_removed_but_names_and_references_stay():
    yaml_text = ("    env:\n    - name: BILLING_DSN\n      value: plain-literal-1\n"
                 "    - name: FROM_SECRET\n      valueFrom:\n        secretKeyRef: {name: s, key: k}\n")
    out = bundle.redact(yaml_text)
    assert "plain-literal-1" not in out and "BILLING_DSN" in out and "secretKeyRef" in out
    json_text = '{"env": [{"name": "BILLING_DSN", "value": "plain-literal-2"}, {"name": "X",\n "value": "with \\"quote\\""}]}'
    out = bundle.redact(json_text)
    assert "plain-literal-2" not in out and "quote" not in out and '"BILLING_DSN"' in out
    assert json.loads(out)["env"][0]["value"] == R


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
