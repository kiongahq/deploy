"""Internal services that hold credentials or the Docker socket fail closed
without MLAIOPS_INTERNAL_TOKEN, so every one of them, and every caller, must
get the same token as the gateway."""

import yaml

from test_sso import ROOT

PROTECTED = ("storage-proxy", "feature-gateway", "serving-manager")


class Loader(yaml.SafeLoader):
    pass


# Compose's !reset tag (used for ports) is irrelevant here.
Loader.add_constructor("!reset", lambda loader, node: None)


def services():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]


def test_protected_services_get_the_gateway_token():
    all_services = services()
    token = all_services["gateway"]["environment"]["MLAIOPS_INTERNAL_TOKEN"]
    for name in PROTECTED:
        assert all_services[name]["environment"].get("MLAIOPS_INTERNAL_TOKEN") == token, name


def test_public_overlay_overrides_every_protected_token():
    public = yaml.load((ROOT / "compose.public.yaml").read_text(), Loader=Loader)["services"]
    for name in PROTECTED:
        assert "MLAIOPS_INTERNAL_TOKEN" in public[name].get("environment", {}), name


def test_shared_agent_runtime_has_matching_invocation_credentials():
    all_services = services()
    gateway = all_services["gateway"]["environment"]
    runtime = all_services["agent-runtime"]["environment"]
    assert gateway["AGENT_RUNTIME_URL"] == "http://agent-runtime:9000"
    assert gateway["KIONGA_AGENT_RUNTIME_TOKEN"] == runtime["MLAIOPS_INTERNAL_TOKEN"]


def test_public_overlay_preserves_shared_runtime_token_pair():
    all_services = services()
    public = yaml.load((ROOT / "compose.public.yaml").read_text(), Loader=Loader)["services"]
    effective = {}
    for name in ("gateway", "agent-runtime"):
        effective[name] = {**all_services[name]["environment"], **public[name].get("environment", {})}
    assert effective["gateway"]["KIONGA_AGENT_RUNTIME_TOKEN"] == effective["agent-runtime"]["MLAIOPS_INTERNAL_TOKEN"]
    # Public preflight rejects an unset token before either side can use a local default.
    assert "MLAIOPS_INTERNAL_TOKEN:?" in public["public-preflight"]["environment"]["MLAIOPS_INTERNAL_TOKEN"]
