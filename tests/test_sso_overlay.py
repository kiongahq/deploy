"""The single sign-on overlay: the gateway and Dex must agree on issuer,
client, redirect URI and connector ids, and images are pinned."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"admin", "operator", "engineer", "user", "viewer"}


@pytest.fixture(scope="module")
def stack():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.sso.yaml"),
         "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
        env={**os.environ, "KIONGA_SRC": str(ROOT.parent)})
    return json.loads(result.stdout)["services"]


@pytest.fixture(scope="module")
def dex():
    return yaml.safe_load((ROOT / "sso/dex.yaml").read_text())


def test_sso_images_are_pinned(stack):
    for name in ("dex", "glauth"):
        assert "@sha256:" in stack[name]["image"], name


def test_gateway_and_dex_agree_on_issuer_client_and_redirect(stack, dex):
    env = stack["gateway"]["environment"]
    assert env["OIDC_ISSUER"] == dex["issuer"]
    client = next(c for c in dex["staticClients"] if c["id"] == env["OIDC_CLIENT_ID"])
    assert env["OIDC_REDIRECT_URL"] in client["redirectURIs"]
    assert stack["dex"]["environment"][client["secretEnv"]] == env["OIDC_CLIENT_SECRET"]
    assert env["OIDC_TOKEN_URL"].startswith("http://dex:5556/") and env["OIDC_JWKS_URL"].startswith("http://dex:5556/")
    assert env["OIDC_AUTH_URL"].startswith(dex["issuer"])  # the browser reaches Dex directly


def test_every_listed_provider_is_a_dex_connector(stack, dex):
    providers = json.loads(stack["gateway"]["environment"]["KIONGA_SSO_PROVIDERS"])
    connectors = {c["id"] for c in dex["connectors"]}
    assert providers and {p["id"] for p in providers} <= connectors


def test_group_role_mapping_names_real_roles(stack):
    mapping = json.loads(stack["gateway"]["environment"]["KIONGA_SSO_GROUP_ROLES"])
    assert mapping and set(mapping.values()) <= ROLES


def test_local_break_glass_stays_on_with_an_explicit_password(stack):
    env = stack["gateway"]["environment"]
    assert env["KIONGA_LOCAL_LOGIN"] == "true"
    assert env.get("MLAIOPS_LOCAL_PASSWORD"), "the gateway refuses local login next to SSO without a password"


def test_ldap_connector_reads_groups_by_member_dn(dex):
    ldap = next(c for c in dex["connectors"] if c["type"] == "ldap")["config"]
    assert ldap["bindPW"].startswith("$")  # from the environment, never inline
    matcher = ldap["groupSearch"]["userMatchers"][0]
    assert matcher == {"userAttr": "DN", "groupAttr": "uniqueMember"}
