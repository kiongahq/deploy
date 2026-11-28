"""The OpenBao secret steps of scripts/e2e-runtime.py and their wiring in the
kind lane: pure helpers, manifests against the CRD schemas, and that the lane
creates no platform Secret with kubectl."""

import base64
import hashlib
import importlib.util
import re
import sys

import pytest
import yaml

from test_production_deploy import ROOT

spec = importlib.util.spec_from_file_location("e2e_runtime_secrets", ROOT / "scripts/e2e-runtime.py")
e2e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e2e  # dataclasses resolve their module
spec.loader.exec_module(e2e)

SETTINGS = e2e.Settings(context="c", namespace="kionga-workloads", base_domain="apps.example", gateway_url="https://g", ca_file=None,
                        agent_image="a@sha256:" + "a" * 64, local_user="admin", local_password="p", model_name="m", model_version="1")


def test_owner_kinds_and_digest_never_need_the_value_in_clear():
    secret = {"metadata": {"ownerReferences": [{"kind": "ExternalSecret", "name": "x"}]},
              "data": {"TOKEN": base64.b64encode(b"value").decode()}}
    assert e2e.owner_kinds(secret) == {"ExternalSecret"}
    assert e2e.owner_kinds({"metadata": {}}) == set() and e2e.owner_kinds(None) == set()
    assert e2e.secret_digest(secret, "TOKEN") == hashlib.sha256(b"value").hexdigest()
    assert e2e.secret_digest(secret, "MISSING") == ""


def deployment(annotations, generation=2, observed=2, updated=2, ready=2, total=2, want=2):
    return {"metadata": {"generation": generation},
            "spec": {"replicas": want, "template": {"metadata": {"annotations": annotations}}},
            "status": {"observedGeneration": observed, "updatedReplicas": updated, "readyReplicas": ready, "replicas": total}}


@pytest.mark.parametrize("current,expected", [
    (deployment({"reloader.stakater.com/last-reloaded-from": "x"}), True),
    (deployment({}), False),  # template unchanged
    (deployment({"r": "x"}, observed=1), False),  # controller has not seen it
    (deployment({"r": "x"}, updated=1), False),  # half rolled
    (deployment({"r": "x"}, total=3), False),  # old pod still terminating
    (deployment({"r": "x"}, ready=1), False),
    (None, False),
])
def test_rolled_out_waits_for_every_replica_on_the_new_template(current, expected):
    assert e2e.rolled_out(current, {}) is expected


def crd_spec(kind):
    for path in (ROOT / "config/crd").glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc["spec"]["names"]["kind"] == kind:
                return doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]


def test_workload_secret_manifests():
    external = e2e.workload_external_secret(SETTINGS, "e2e-openbao", "workloads/e2e-openbao")
    assert external["metadata"]["namespace"] == "kionga-workloads"
    assert external["spec"]["target"]["template"]["metadata"]["labels"] == {"kionga.dev/workload-secret": "true"}
    assert external["spec"]["secretStoreRef"] == {"name": "kionga-store", "kind": "SecretStore"}
    assert external["spec"]["data"][0]["remoteRef"]["key"].startswith("workloads/")
    app = e2e.secret_app_manifest(SETTINGS, "e2e-secret-app", "e2e-openbao")
    schema = crd_spec("KiongaApplication")
    assert set(app["spec"]) <= set(schema["properties"])
    assert app["spec"]["secretRefs"] == [{"name": "e2e-openbao"}]


def test_secret_steps_are_separate_from_the_runtime_steps():
    assert e2e.SECRET_STEPS == ["secrets", "secret-rotation", "workload-secret"]
    assert not set(e2e.SECRET_STEPS) & set(e2e.STEPS)
    for step in e2e.SECRET_STEPS:
        assert callable(getattr(e2e.Runner, "step_" + step.replace("-", "_")))


def test_secret_steps_need_the_key_file(tmp_path):
    runner = e2e.Runner(SETTINGS, kube=None, http_client=None)
    with pytest.raises(e2e.StepFailed, match="openbao-key-file"):
        runner.bootstrap("seed")


def test_cli_accepts_secret_steps(tmp_path, monkeypatch):
    password = tmp_path / "pw"
    password.write_text("x")
    ran = []
    monkeypatch.setattr(e2e, "run_steps", lambda runner, selected: ran.extend(selected) or [])
    assert e2e.main(["--context", "c", "--base-domain", "d", "--gateway-url", "g", "--agent-image", "a", "--local-password-file", str(password),
                     "--openbao-key-file", "k", "--steps", "secrets,secret-rotation,workload-secret"]) == 0
    assert ran == e2e.SECRET_STEPS


def test_workflow_runs_the_secret_steps_with_the_key_file():
    workflow = yaml.safe_load((ROOT / ".github/workflows/kubernetes-e2e.yml").read_text())
    steps = workflow["jobs"]["e2e"]["steps"]
    secret_step = next(s for s in steps if s.get("name") == "Run the OpenBao secret steps")
    assert '--openbao-key-file "$OUT/openbao-init.json"' in secret_step["run"]
    assert "!cancelled()" in secret_step["if"]
    assert set(secret_step["env"]["SECRET_STEPS"].split("'")[1].split(",")) == set(e2e.SECRET_STEPS)
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert "openbao-init.json" not in upload["with"]["path"], "the key shares never leave the runner"


def test_kind_lane_creates_no_platform_secret_with_kubectl():
    setup = (ROOT / "scripts/kind-ci-up.sh").read_text()
    created = re.findall(r"create secret generic (\S+)", setup)
    assert set(created) == {"postgres", "objectstore"}, created
    assert all("-n kionga-data" in line for line in setup.splitlines() if "create secret" in line)
    assert "openbao-bootstrap.sh" in setup and "seed" in setup and "--set-file \"secretStore.openbao.caBundle=" in setup
    values = yaml.safe_load((ROOT / "kind/ci/values.yaml").read_text())
    assert values["secretStore"]["openbao"]["enabled"] and values["externalSecrets"]["enabled"]
    targets = set(values["externalSecrets"]["targets"]) | set(values["externalSecrets"]["workspaceTargets"])
    assert targets == {name for names in e2e.PLATFORM_SECRETS.values() for name in names}
    gateway_keys = {t["secretKey"] for t in values["externalSecrets"]["targets"]["kionga-gateway"]}
    assert gateway_keys == set(values["components"]["gateway"]["secrets"])
    openbao = yaml.safe_load((ROOT / "kind/ci/openbao-values.yaml").read_text())["server"]
    assert "dev" not in openbao and "ha" not in openbao, "kind keeps the real Raft cluster from the defaults"
