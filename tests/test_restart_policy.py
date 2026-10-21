"""After Docker or the machine restarts, the local stack resumes whole.

Only pipeline-runner and realtime-processor used to restart, so a Docker
restart left them crash-looping against a stopped Prefect and Kafka. Every
long-running service now restarts unless stopped; `make local-down` still
removes them. One-shot jobs and opt-in test targets never restart."""

import yaml

from test_sso import ROOT

ONE_SHOT = {"objectstore-init", "feature-materializer"}
TEST_TARGETS = {"elasticsearch", "opensearch"}


def test_long_running_services_resume_after_a_restart():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name, service in services.items():
        if name in ONE_SHOT:
            assert service.get("restart", "no") == "no", f"{name} is a one-shot job"
        elif name not in TEST_TARGETS:
            assert service.get("restart") == "unless-stopped", f"{name} would stay down after a Docker restart"


def test_one_shot_jobs_exist():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    assert ONE_SHOT | TEST_TARGETS <= set(services)
