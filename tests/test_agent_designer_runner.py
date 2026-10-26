"""The Agent Designer runner executes user-built graphs, so it is the most
exposed workload in the stack: it must stay unreachable from the host, run
without capabilities on a read-only filesystem, and share its credential with
the gateway that calls it."""

import yaml

from test_sso import ROOT

SERVICES = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
RUNNER = SERVICES["agent-designer-runner"]
GATEWAY = SERVICES["gateway"]["environment"]


def test_runner_is_internal_and_hardened():
    assert not RUNNER.get("ports"), "the runner must not publish a host port"
    assert RUNNER["read_only"] is True
    assert RUNNER["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in RUNNER["security_opt"]
    assert RUNNER["pids_limit"] <= 512 and RUNNER["mem_limit"]
    assert RUNNER["entrypoint"] == ["python", "-m", "agent_runtime.designer"]
    assert "volumes" not in RUNNER, "the runner needs no host mounts (and never the Docker socket)"


def test_gateway_and_runner_share_one_credential_and_callback():
    runner_env = RUNNER["environment"]
    assert GATEWAY["KIONGA_AGENT_DESIGNER_RUNNER_URL"] == "http://agent-designer-runner:9100"
    assert GATEWAY["KIONGA_AGENT_DESIGNER_RUNNER_TOKEN"] == runner_env["KIONGA_DESIGNER_RUNNER_TOKEN"]
    assert GATEWAY["KIONGA_AGENT_DESIGNER_CALLBACK_URL"] == "http://gateway:8087/api/v1"
    assert runner_env["MLAIOPS_LLM_BACKEND"].endswith(":-mock}"), "keyless local stacks run with the mock model"
