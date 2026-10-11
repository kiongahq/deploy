"""Sign-in wiring: the local stack starts every method (Dex with LDAP, a demo
OAuth account and any configured OAuth provider, plus local accounts), the
gateway and Dex agree on issuer, client, redirect and connectors, and the
public overlay keeps the same Dex with the local-only parts off.

The Dex config is a gomplate template; these tests render it with the Dex
image's own gomplate and the environment Compose would give the container."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"admin", "operator", "engineer", "user", "viewer"}
PUBLIC_ENV = {
    "MLAIOPS_DOMAIN": "kionga.example.com", "MLAIOPS_ACME_EMAIL": "ops@example.com",
    "DEX_CLIENT_SECRET": "public-client-secret", "DEX_ADMIN_EMAIL": "admin@example.com",
    "DEX_ADMIN_PASSWORD_HASH": "$2y$10$hash", "MLAIOPS_INTERNAL_TOKEN": "internal",
    "MLAIOPS_LOCAL_PASSWORD": "break-glass",
}
OAUTH_ENV = {
    "KIONGA_GITHUB_CLIENT_ID": "gh-id", "KIONGA_GITHUB_CLIENT_SECRET": "gh-secret-value",
    "KIONGA_GITHUB_ORG": "kiongahq",
    "KIONGA_GOOGLE_CLIENT_ID": "google-id", "KIONGA_GOOGLE_CLIENT_SECRET": "google-secret-value",
    "KIONGA_GITLAB_CLIENT_ID": "gitlab-id", "KIONGA_GITLAB_CLIENT_SECRET": "gitlab-secret-value",
    "KIONGA_MICROSOFT_CLIENT_ID": "ms-id", "KIONGA_MICROSOFT_CLIENT_SECRET": "ms-secret-value",
}


def compose_config(*files, env=None):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("KIONGA_", "DEX_", "MLAIOPS_", "OIDC_"))}
    args = ["docker", "compose"]
    for name in files:
        args += ["-f", str(ROOT / name)]
    result = subprocess.run(
        [*args, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True,
        env={**clean, "KIONGA_SRC": str(ROOT.parent), **(env or {})})
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)["services"]


def render_dex(service):
    """Run the Dex container's own render step; return (config, providers)."""
    image = service["image"]
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        pytest.skip(f"{image} is not pulled (make local-up pulls it)")
    env = [arg for key, value in service["environment"].items() for arg in ("-e", f"{key}={value}")]
    script = ("gomplate -f /etc/kionga-sso/dex.yaml -o /tmp/dex.yaml && cat /tmp/dex.yaml && echo '#---' && "
              "gomplate -d dex=/tmp/dex.yaml -f /etc/kionga-sso/providers.tmpl")
    result = subprocess.run(
        ["docker", "run", "--rm", *env, "-v", f"{ROOT / 'sso'}:/etc/kionga-sso:ro", "--entrypoint", "sh", image, "-c", script],
        capture_output=True, text=True, check=True)
    config, providers = result.stdout.split("#---\n")
    return config, json.loads(providers)


@pytest.fixture(scope="module")
def local():
    return compose_config("compose.yaml")


@pytest.fixture(scope="module")
def local_render(local):
    return render_dex(local["dex"])


@pytest.fixture(scope="module")
def oauth_render():
    return render_dex(compose_config("compose.yaml", env=OAUTH_ENV)["dex"])


@pytest.fixture(scope="module")
def public():
    return compose_config("compose.yaml", "compose.public.yaml", env=PUBLIC_ENV)


@pytest.fixture(scope="module")
def public_render(public):
    return render_dex(public["dex"])


def test_sign_in_images_are_pinned(local):
    for name in ("dex", "glauth"):
        assert "@sha256:" in local[name]["image"], name


def test_local_up_starts_every_sign_in_method(local, local_render):
    config, providers = local_render
    assert [p["id"] for p in providers] == ["ldap", "mock"]
    env = local["gateway"]["environment"]
    assert env["KIONGA_LOCAL_LOGIN"] == "true" and env["MLAIOPS_LOCAL_PASSWORD"]
    assert env["KIONGA_SIGNUP"] == "approval"
    assert local["gateway"]["depends_on"]["dex"]["condition"] == "service_healthy"
    assert "glauth" in local and "profiles" not in local["glauth"]


def test_gateway_and_dex_agree_on_issuer_client_redirect_and_secret(local, local_render):
    dex = yaml.safe_load(local_render[0])
    env = local["gateway"]["environment"]
    assert env["OIDC_ISSUER"] == dex["issuer"]
    client = next(c for c in dex["staticClients"] if c["id"] == env["OIDC_CLIENT_ID"])
    assert env["OIDC_REDIRECT_URL"] in client["redirectURIs"]
    assert local["dex"]["environment"][client["secretEnv"]] == env["OIDC_CLIENT_SECRET"]
    assert env["OIDC_TOKEN_URL"].startswith("http://dex:5556/") and env["OIDC_JWKS_URL"].startswith("http://dex:5556/")
    assert env["OIDC_AUTH_URL"] == dex["issuer"] + "/auth"  # the browser reaches Dex directly
    assert "federated:id" in env["OIDC_SCOPES"].split()  # tokens name the connector, for sign-up records


def test_the_login_page_list_reaches_the_gateway_read_only(local):
    provider_file = Path(local["gateway"]["environment"]["KIONGA_SSO_PROVIDERS_FILE"])
    mounts = {m["target"]: m for m in local["gateway"]["volumes"]}
    shared = mounts[str(provider_file.parent)]
    assert shared["read_only"] and shared["source"] == "sso-providers"
    dex_mount = next(m for m in local["dex"]["volumes"] if m.get("source") == "sso-providers")
    assert dex_mount["target"] == "/var/dex"  # the image's dex-owned dir, so Dex can write it
    assert "/var/dex/providers.json" in " ".join(local["dex"]["command"])
    assert "mv /var/dex/providers.json.tmp /var/dex/providers.json" in " ".join(local["dex"]["command"])  # atomic


def test_oauth_providers_appear_only_when_configured(oauth_render):
    config, providers = oauth_render
    assert [p["id"] for p in providers] == ["ldap", "github", "google", "gitlab", "microsoft", "mock"]
    dex = yaml.safe_load(config)
    connectors = {c["id"]: c for c in dex["connectors"]}
    for name in ("github", "google", "gitlab", "microsoft"):
        assert connectors[name]["config"]["redirectURI"] == dex["issuer"] + "/callback"
    assert connectors["github"]["config"]["orgs"] == [{"name": "kiongahq"}]


def test_rendered_config_never_holds_a_secret(oauth_render):
    config, _ = oauth_render
    for key, value in OAUTH_ENV.items():
        if key.endswith("_SECRET"):
            assert value not in config, key
            assert f"${key}" in config  # Dex expands it when it loads the config


def test_every_listed_provider_is_a_dex_connector(local_render, oauth_render, public_render):
    for config, providers in (local_render, oauth_render, public_render):
        dex = yaml.safe_load(config)
        connectors = {c["id"] for c in dex.get("connectors") or []}
        if dex.get("enablePasswordDB"):
            connectors.add("local")  # Dex's built-in password connector
        assert {p["id"] for p in providers} == connectors


def test_group_role_mapping_names_real_roles(local):
    mapping = json.loads(local["gateway"]["environment"]["KIONGA_SSO_GROUP_ROLES"])
    assert mapping and set(mapping.values()) <= ROLES


def test_ldap_connector_reads_groups_by_member_dn(local_render):
    dex = yaml.safe_load(local_render[0])
    ldap = next(c for c in dex["connectors"] if c["type"] == "ldap")["config"]
    assert ldap["bindPW"].startswith("$")  # from the environment, never inline
    assert ldap["groupSearch"]["userMatchers"][0] == {"userAttr": "DN", "groupAttr": "uniqueMember"}


def test_public_overlay_keeps_dex_without_the_local_only_parts(public, public_render):
    config, providers = public_render
    dex = yaml.safe_load(config)
    assert dex["issuer"] == "https://kionga.example.com/dex"
    assert dex["staticClients"][0]["redirectURIs"] == ["https://kionga.example.com/auth/callback"]
    assert [p["id"] for p in providers] == ["local"]  # no test directory, no demo account
    assert "glauth" not in public  # behind the local-directory profile
    env = public["gateway"]["environment"]
    assert env["OIDC_ISSUER"] == dex["issuer"] and env["OIDC_CLIENT_SECRET"] == "public-client-secret"
    assert env["MLAIOPS_LOCAL_PASSWORD"] == "break-glass"


def test_public_overlay_requires_a_break_glass_password():
    env = {k: v for k, v in PUBLIC_ENV.items() if k != "MLAIOPS_LOCAL_PASSWORD"}
    with pytest.raises(AssertionError, match="MLAIOPS_LOCAL_PASSWORD"):
        compose_config("compose.yaml", "compose.public.yaml", env=env)


def test_public_overlay_publishes_only_the_tls_edge(public):
    published = sorted(name for name, service in public.items() if service.get("ports"))
    assert published == ["caddy"]
