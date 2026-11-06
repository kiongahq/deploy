"""Internal services that hold credentials or the Docker socket fail closed
without MLAIOPS_INTERNAL_TOKEN, so every one of them, and every caller, must
get the same token as the gateway."""

import pytest
import yaml

from test_sso import PUBLIC_ENV, ROOT, compose_config

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


@pytest.mark.parametrize("files,env", [
    (("compose.yaml",), {"MLAIOPS_INTERNAL_TOKEN": "rendered-shared-runtime-token"}),
    (("compose.yaml", "compose.public.yaml"), PUBLIC_ENV),
])
def test_rendered_shared_agent_runtime_has_matching_invocation_credentials(files, env):
    rendered = compose_config(*files, env=env)
    gateway = rendered["gateway"]["environment"]
    runtime = rendered["agent-runtime"]["environment"]
    assert gateway["AGENT_RUNTIME_URL"] == "http://agent-runtime:9000"
    assert gateway["KIONGA_AGENT_RUNTIME_TOKEN"] == runtime["MLAIOPS_INTERNAL_TOKEN"]


def test_public_preflight_requires_the_shared_runtime_token():
    public = yaml.load((ROOT / "compose.public.yaml").read_text(), Loader=Loader)["services"]
    # Public preflight rejects an unset token before either side can use a local default.
    assert "MLAIOPS_INTERNAL_TOKEN:?" in public["public-preflight"]["environment"]["MLAIOPS_INTERNAL_TOKEN"]


def test_mlflow_names_the_postgres_driver_its_image_installs():
    """SQLAlchemy 2.1 made psycopg the default for postgresql://, and the image has psycopg2."""
    uri = services()["mlflow"]["environment"]["BACKEND_STORE_URI"]
    assert uri.startswith("postgresql+psycopg2://")
