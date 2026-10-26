"""Helm: the Agent Designer runner is opt-in, hardened, and fenced by its own
NetworkPolicy. It may receive only from the gateway and reach only DNS, the
gateway's internal listener and the trace-proxy."""

import subprocess

import pytest

from test_production_deploy import ROOT, helm_binary, render_chart

RUNNER = "kionga-agent-designer-runner"


def find(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def gateway_container(docs):
    return find(docs, "Deployment", "kionga-gateway")["spec"]["template"]["spec"]["containers"][0]


def env(container):
    return {item["name"]: item.get("value") for item in container["env"]}


def test_disabled_by_default_and_native_image_still_reaches_the_gateway(tmp_path):
    docs = render_chart(tmp_path)
    assert not any(d["metadata"]["name"] == RUNNER for d in docs)
    gateway = env(gateway_container(docs))
    assert "KIONGA_AGENT_DESIGNER_RUNNER_URL" not in gateway
    assert "@sha256:" in gateway["KIONGA_NATIVE_AGENT_IMAGE"]


def test_enabled_runner_is_hardened_and_shares_the_token_with_the_gateway(tmp_path):
    docs = render_chart(tmp_path, agentDesigner={"enabled": True, "replicas": 2, "maxRuns": 4, "secretName": "designer-token",
                                                   "llm": {"backend": "openai-compatible", "model": "gpt-x"},
                                                   "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}})
    deployment = find(docs, "Deployment", RUNNER)
    pod = deployment["spec"]["template"]["spec"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert "app.kubernetes.io/instance" not in labels, "the broad control-plane policy must not select runner pods"
    container = pod["containers"][0]
    assert "@sha256:" in container["image"] and container["command"] == ["python", "-m", "agent_runtime.designer"]
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    assert pod["securityContext"]["runAsNonRoot"] and pod["automountServiceAccountToken"] is False
    runner_env = env(container)
    assert runner_env["KIONGA_DESIGNER_RUNNER_TOKEN_FILE"] == "/run/secrets/designer/KIONGA_DESIGNER_RUNNER_TOKEN"
    assert "KIONGA_DESIGNER_RUNNER_TOKEN" not in runner_env and not any("API_KEY" in name for name in runner_env)
    assert runner_env["MLAIOPS_LLM_BASE_URL"] == "http://kionga-trace-proxy.kionga-system.svc:8081/v1"
    assert runner_env["KIONGA_DESIGNER_MAX_RUNS"] == "4"
    assert pod["volumes"][1]["secret"]["secretName"] == "designer-token"
    assert find(docs, "Service", RUNNER)["spec"]["type"] == "ClusterIP"
    assert find(docs, "PodDisruptionBudget", RUNNER)

    gateway = gateway_container(docs)
    gateway_env = env(gateway)
    assert gateway_env["KIONGA_AGENT_DESIGNER_RUNNER_URL"] == "http://kionga-agent-designer-runner.kionga-system.svc:9100"
    assert gateway_env["KIONGA_AGENT_DESIGNER_CALLBACK_URL"] == "http://kionga-gateway.kionga-system.svc:8087/api/v1"
    assert gateway_env["KIONGA_AGENT_DESIGNER_RUNNER_TOKEN_FILE"] == "/run/designer/KIONGA_DESIGNER_RUNNER_TOKEN"
    assert any(m["mountPath"] == "/run/designer" and m["readOnly"] for m in gateway["volumeMounts"])


def test_runner_network_policy_allows_only_the_gateway_and_trace_proxy(tmp_path):
    docs = render_chart(tmp_path, agentDesigner={"enabled": True, "replicas": 2, "maxRuns": 8, "secretName": "t",
                                                   "llm": {"backend": "openai-compatible", "model": ""}, "resources": {}})
    policy = find(docs, "NetworkPolicy", RUNNER)["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    [ingress] = policy["ingress"]
    assert ingress["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"
    assert ingress["ports"] == [{"port": 9100}]
    targets = sorted((rule["to"][0].get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component", "dns"),
                      tuple(port["port"] for port in rule["ports"])) for rule in policy["egress"])
    assert targets == [("dns", (53, 53)), ("gateway", (8087,)), ("trace-proxy", (8081,))]
    control = find(docs, "NetworkPolicy", "kionga-control")["spec"]
    runner_selector = {"kionga.dev/release": "kionga", "app.kubernetes.io/component": "agent-designer-runner"}
    assert any(rule.get("ports") == [{"port": 9100}] and rule["to"][0].get("podSelector", {}).get("matchLabels") == runner_selector
               for rule in control["egress"]), "the gateway must be allowed to reach the runner"
    assert any(rule["from"][0].get("podSelector", {}).get("matchLabels") == runner_selector
               and rule["ports"] == [{"port": 8087}, {"port": 8081}] for rule in control["ingress"])


def test_single_replica_runner_is_refused(tmp_path):
    with pytest.raises(AssertionError, match="at least 2"):
        render_chart(tmp_path, agentDesigner={"enabled": True, "replicas": 1, "maxRuns": 8, "secretName": "t",
                                               "llm": {"backend": "openai-compatible", "model": ""}, "resources": {}})
