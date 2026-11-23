"""Workflows that boot compose.yaml must provide what it requires: it has no
PostgreSQL default, and the nightly Pressure run failed on every job without one."""

import re

import yaml

from test_production_deploy import ROOT


def test_every_compose_job_in_pressure_generates_a_postgres_password():
    workflow = yaml.safe_load((ROOT / ".github/workflows/pressure.yml").read_text())
    required = set(re.findall(r"\$\{(POSTGRES_PASSWORD):\?", (ROOT / "compose.yaml").read_text()))
    assert required == {"POSTGRES_PASSWORD"}
    for name, job in workflow["jobs"].items():
        scripts = "\n".join(step.get("run", "") for step in job["steps"])
        if "docker compose" not in scripts:
            continue
        assert re.search(r'postgres_password="\$\(openssl rand -hex \d+\)"', scripts), name
        assert 'echo "POSTGRES_PASSWORD=$postgres_password"' in scripts and '::add-mask::$postgres_password' in scripts, name
        assert scripts.index("POSTGRES_PASSWORD=") < scripts.index("docker compose"), name
