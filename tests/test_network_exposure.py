"""Local mode never publishes a port beyond loopback by default.

The local stack ships well-known development credentials (Postgres, the object
store, Langfuse, the internal service token), so a port bound to every interface
hands them to anyone on the same network. Every host port in compose.yaml binds
to KIONGA_BIND_ADDRESS (default 127.0.0.1); the workbench and log exporter are
pinned to loopback even when that is widened."""

import pytest
import yaml

from test_sso import ROOT, compose_config

BIND = "${KIONGA_BIND_ADDRESS:-127.0.0.1}:"
ALWAYS_LOOPBACK = {"jupyter", "log-exporter", "elasticsearch", "opensearch"}


def published(services):
    return {name: service["ports"] for name, service in services.items() if service.get("ports")}


def test_every_published_port_names_a_bind_address():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name, ports in published(services).items():
        for port in ports:
            allowed = ("127.0.0.1:",) if name in ALWAYS_LOOPBACK else (BIND, "127.0.0.1:")
            assert str(port).startswith(allowed), f"{name} publishes {port!r} without a loopback default"


def test_local_stack_binds_to_loopback_by_default():
    for name, ports in published(compose_config("compose.yaml")).items():
        for port in ports:
            assert port["host_ip"] == "127.0.0.1", f"{name} publishes {port['published']} on {port['host_ip']}"


def test_bind_address_widens_only_the_shareable_services():
    services = compose_config("compose.yaml", env={"KIONGA_BIND_ADDRESS": "0.0.0.0"})
    exposed = {name for name, ports in published(services).items() if any(p["host_ip"] == "0.0.0.0" for p in ports)}
    assert "gateway" in exposed
    assert not exposed & ALWAYS_LOOPBACK


@pytest.mark.parametrize("service", sorted(ALWAYS_LOOPBACK))
def test_pinned_services_exist(service):
    # A renamed service would silently drop out of ALWAYS_LOOPBACK.
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    if service not in services:
        pytest.fail(f"{service} is not in compose.yaml; update ALWAYS_LOOPBACK")


def test_log_exporter_reads_the_deploy_secrets_folder_and_every_setting():
    exporter = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["log-exporter"]
    assert "./config/secrets:/run/kionga-secrets:ro" in exporter["volumes"]
    assert (ROOT / "config/secrets/README").is_file()
    for name in ("KIONGA_LOG_EXPORT_INSECURE_TLS", "KIONGA_LOG_EXPORT_BATCH"):
        assert name in exporter["environment"], f"{name} is read by the exporter but not passed through"
