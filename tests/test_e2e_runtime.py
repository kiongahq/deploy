"""scripts/e2e-runtime.py: the parsing, polling and load accounting the live
Kubernetes lane depends on, without a cluster."""

import importlib.util
import json
import sys

import pytest
import yaml

from test_production_deploy import ROOT

spec = importlib.util.spec_from_file_location("e2e_runtime", ROOT / "scripts/e2e-runtime.py")
e2e = importlib.util.module_from_spec(spec)
sys.modules["e2e_runtime"] = e2e
spec.loader.exec_module(e2e)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def ready(status="True", **extra):
    return {"status": dict({"phase": "Ready", "conditions": [{"type": "Ready", "status": status, "reason": "R", "message": "m"}]}, **extra)}


def test_conditions_and_descriptions():
    assert e2e.is_ready(ready()) and not e2e.is_ready(ready("False")) and not e2e.is_ready(None)
    assert e2e.condition({"status": {}}) is None
    assert e2e.describe(None) == "not found"
    assert e2e.describe(ready("False")) == "phase=Ready Ready=False(R: m)"


def test_replicas_treats_missing_fields_as_zero():
    assert e2e.replicas(None) == (0, 0)
    assert e2e.replicas({"spec": {"replicas": 0}, "status": {}}) == (0, 0)
    assert e2e.replicas({"spec": {"replicas": 3}, "status": {"readyReplicas": 2}}) == (3, 2)


def test_owned_by_prefers_owner_references_then_name():
    items = [{"metadata": {"name": "e2e-web"}},
             {"metadata": {"name": "e2e-web-abc", "ownerReferences": [{"kind": "KiongaApplication", "name": "e2e-web"}]}}]
    assert e2e.owned_by(items, "KiongaApplication", "e2e-web")["metadata"]["name"] == "e2e-web-abc"
    assert e2e.owned_by(items[:1], "KiongaApplication", "e2e-web")["metadata"]["name"] == "e2e-web"
    assert e2e.owned_by(items, "KiongaFunction", "other") is None


def test_http_hosts_reads_both_keda_http_shapes():
    owner = {"ownerReferences": [{"kind": "KiongaFunction", "name": "fn"}]}
    scaled = [{"metadata": dict(owner, name="x"), "spec": {"hosts": ["fn.kionga-workloads"]}}]
    route = [{"metadata": dict(owner, name="y"), "spec": {"rules": [{"hosts": ["a"]}, {"host": "b"}]}}]
    assert e2e.http_hosts(scaled, "KiongaFunction", "fn") == ["fn.kionga-workloads"]
    assert e2e.http_hosts(route, "KiongaFunction", "fn") == ["a", "b"]
    assert e2e.http_hosts([], "KiongaFunction", "fn") == []


@pytest.mark.parametrize("url,expected", [
    ("http://iris-predictor.kionga-workloads.svc.cluster.local", ("iris-predictor", "kionga-workloads", 80)),
    ("https://iris-predictor.ns.svc:8443/v1/models/iris", ("iris-predictor", "ns", 8443)),
])
def test_service_from_url(url, expected):
    assert e2e.service_from_url(url) == expected


def test_service_from_url_rejects_external_hosts():
    with pytest.raises(ValueError):
        e2e.service_from_url("https://iris.apps.example.com")


@pytest.mark.parametrize("url,expected", [
    ("http://iris-predictor.ns.svc.cluster.local", "/v1/models/e2e-iris:predict"),
    ("http://iris-predictor.ns.svc.cluster.local/", "/v1/models/e2e-iris:predict"),
    ("http://x.ns.svc/v1/models/custom", "/v1/models/custom:predict"),
    ("http://x.ns.svc/v1/models/custom:predict", "/v1/models/custom:predict"),
])
def test_predict_path(url, expected):
    assert e2e.predict_path(url, "e2e-iris") == expected


@pytest.mark.parametrize("status,state", [("succeeded", "success"), ("Completed", "success"), ("failed", "failure"),
                                          ("cancelled", "failure"), ("running", "running"), ("queued", "running"), (None, "running")])
def test_pipeline_state(status, state):
    assert e2e.pipeline_state({"status": status}) == state


def test_poll_returns_the_first_truthy_value_and_treats_errors_as_not_yet():
    clock, values = FakeClock(), [None, RuntimeError("api down"), 0, {"ok": 1}]

    def check():
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    assert e2e.poll(check, 60, 2, clock=clock, sleep=clock.sleep) == {"ok": 1}
    assert clock.now == 6


def test_poll_times_out_with_the_last_observation():
    clock = FakeClock()
    with pytest.raises(e2e.StepFailed, match=r"timed out after 10s waiting for scale to zero; last: \(1, 1\)"):
        e2e.poll(lambda: None, 10, 3, what="scale to zero", clock=clock, sleep=clock.sleep,
                 describe_last=lambda _: "(1, 1)")
    assert 10 <= clock.now <= 12


def test_load_counts_successes_and_groups_errors():
    result = e2e.LoadResult()
    for outcome in ["200", "200", "503", "TimeoutError", "503"]:
        result.record(outcome)
    assert (result.sent, result.ok, result.errors) == (5, 2, {"503": 2, "TimeoutError": 1})
    assert result.success_rate == 0.4
    assert e2e.LoadResult().success_rate == 0.0


def test_drive_load_stops_at_the_deadline():
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return ticks["n"] / 1000

    def request():
        if ticks["n"] % 7 == 0:
            raise TimeoutError()
        return "200"
    result = e2e.drive_load(request, 2, 0.05, clock=clock)
    assert result.sent > 0 and result.sent == result.ok + sum(result.errors.values())


def test_manifests_are_valid_against_the_crd_schemas():
    settings = e2e.Settings(context="c", namespace="kionga-workloads", base_domain="apps.127.0.0.1.sslip.io", gateway_url="https://g",
                            ca_file=None, agent_image="a@sha256:" + "a" * 64, local_user="admin", local_password="p",
                            model_name="e2e-iris", model_version="1")
    crds = {}
    for path in (ROOT / "config/crd").glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                crds[doc["spec"]["names"]["kind"]] = doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    for manifest in (e2e.application_manifest(settings), e2e.function_manifest(settings)):
        schema = crds[manifest["kind"]]
        assert set(manifest["spec"]) <= set(schema["properties"]), manifest["kind"]
        assert manifest["spec"]["scaling"]["metric"] in schema["properties"]["scaling"]["properties"]["metric"]["enum"]
    assert e2e.function_manifest(settings)["spec"]["replicas"]["min"] == 0
    assert "@sha256:" in e2e.WHOAMI and "@sha256:" in e2e.BUSYBOX


class FakeRunner:
    def __init__(self, fail=()):
        self.fail, self.ran = fail, []

    def __getattr__(self, name):
        step = name.removeprefix("step_").replace("_", "-")

        def run():
            self.ran.append(step)
            if step in self.fail:
                raise e2e.StepFailed(f"{step} broke")
            return f"{step} ok"
        return run


def test_run_steps_continues_after_a_failure_and_skips_dependents():
    runner = FakeRunner(fail={"application", "model"})
    results = e2e.run_steps(runner, e2e.STEPS)
    status = {r["step"]: r["status"] for r in results}
    assert status == {"application": "failed", "recovery": "skipped", "scale-up": "skipped", "function": "passed",
                      "agent": "passed", "model": "failed", "pipeline": "passed"}
    assert "recovery" not in runner.ran
    assert json.dumps(results)


def test_cli_rejects_unknown_steps(tmp_path):
    password = tmp_path / "pw"
    password.write_text("x")
    with pytest.raises(SystemExit):
        e2e.main(["--context", "c", "--base-domain", "d", "--gateway-url", "g", "--agent-image", "a", "--local-password-file", str(password),
                  "--steps", "application,teleport"])
