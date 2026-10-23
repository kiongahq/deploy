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
