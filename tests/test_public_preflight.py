"""Public mode refuses the credentials the local stack ships with.

compose.yaml defaults every credential (Langfuse admin password and keys, the
Postgres password, object store keys, workspace and service tokens) so a laptop
boots with no setup. Caddy publishes Langfuse at /langfuse, so a public overlay
that kept those defaults let anyone who has read this repository sign in.

The rules live in one place, scripts/public-preflight.sh, which public-up.sh runs
on the host and the public-preflight service runs inside Compose, so a direct
`docker compose -f compose.yaml -f compose.public.yaml up` is covered too. The
local defaults are read from compose.yaml itself: a new defaulted credential that
is not overridden or rejected in public mode fails these tests."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

from test_sso import PUBLIC_ENV, ROOT, compose_config

PREFLIGHT = ROOT / "scripts" / "public-preflight.sh"
CREDENTIAL = re.compile(r"(PASSWORD|SECRET|TOKEN|ACCESS_KEY|PUBLIC_KEY|_KEY|SALT)$")
DEFAULTED = re.compile(r"\$\{([A-Z0-9_]+):-([^}]+)\}")
DSN_PASSWORD = re.compile(r"://[^:/@]+:([^@]+)@")
STARTS_WITH_PREFLIGHT = {"caddy", "dex", "gateway", "postgres", "objectstore", "langfuse", "jupyter"}


def local_defaults():
    """{VAR: default} for every credential compose.yaml gives a non-empty default."""
    found = {}
    for var, default in DEFAULTED.findall((ROOT / "compose.yaml").read_text()):
        if CREDENTIAL.search(var):
            found[var] = default
    return found


LOCAL_DEFAULTS = local_defaults()


def preflight_table():
    out = subprocess.run(["sh", str(PREFLIGHT), "--list"], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.split())


def run_preflight(env, shell="sh"):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("KIONGA_", "DEX_", "KIONGA_", "LANGFUSE_", "LANGFLOW_"))}
    for name in preflight_table():
        clean.pop(name, None)
    return subprocess.run([shell, str(PREFLIGHT)], capture_output=True, text=True, env={**clean, **env})


def test_local_defaults_are_found():
    # Guards the parser: these are the defaults the issue was filed about.
    assert LOCAL_DEFAULTS["LANGFUSE_INIT_USER_PASSWORD"] == "kionga-local-admin"
    assert LOCAL_DEFAULTS["LANGFUSE_NEXTAUTH_SECRET"] == "kionga-local-nextauth"
    assert "POSTGRES_PASSWORD" not in LOCAL_DEFAULTS
    assert LOCAL_DEFAULTS["OBJECTSTORE_SECRET_KEY"] == "kionga-local-secret"


def test_compose_yaml_has_no_inline_credentials():
    # A literal credential cannot be overridden from .env, so public mode could
    # never replace it. Every one goes through a ${VAR:-default}.
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name, service in services.items():
        env = service.get("environment") or {}
        for key, value in env.items():
            value = str(value)
            if CREDENTIAL.search(key) and value:
                assert value.startswith("${"), f"{name}.{key} is an inline credential"
            for password in DSN_PASSWORD.findall(value):
                assert password.startswith("${"), f"{name}.{key} embeds an inline database password"


def test_preflight_knows_every_local_default():
    table = preflight_table()
    # DEX_KIONGA_CLIENT_SECRET is replaced by DEX_CLIENT_SECRET in the overlay.
    for var, default in LOCAL_DEFAULTS.items():
        if var == "DEX_KIONGA_CLIENT_SECRET":
            assert table["DEX_CLIENT_SECRET"] == default
            continue
        assert table.get(var) == default, f"{var} defaults to {default!r} in compose.yaml; add it to public-preflight.sh"


@pytest.fixture(scope="module")
def public():
    return compose_config("compose.yaml", "compose.public.yaml", env=PUBLIC_ENV)


def test_public_overlay_resolves_no_local_default(public):
    defaults = set(LOCAL_DEFAULTS.values())
    for name, service in public.items():
        for key, value in (service.get("environment") or {}).items():
            value = str(value)
            if CREDENTIAL.search(key):
                assert value not in defaults, f"{name}.{key} keeps a local default in public mode"
            for password in DSN_PASSWORD.findall(value):
                assert password not in defaults, f"{name}.{key} connects with a local default password"


@pytest.mark.parametrize("var", sorted(set(preflight_table()) - {"DEX_LDAP_BIND_PASSWORD"}))
def test_public_overlay_fails_config_when_a_credential_is_unset(var):
    env = {k: v for k, v in PUBLIC_ENV.items() if k != var}
    with pytest.raises(AssertionError, match=var):
        compose_config("compose.yaml", "compose.public.yaml", env=env)


def test_credential_services_wait_for_the_preflight(public):
    assert public["public-preflight"]["entrypoint"] == ["/bin/sh", "/kionga/public-preflight.sh"]
    for name in STARTS_WITH_PREFLIGHT:
        condition = public[name]["depends_on"]["public-preflight"]["condition"]
        assert condition == "service_completed_successfully", name


def test_public_dex_never_gets_the_test_directory_bind_password(public):
    assert public["dex"]["environment"]["DEX_LDAP_BIND_PASSWORD"] == ""


def test_preflight_passes_with_generated_values():
    result = run_preflight(PUBLIC_ENV)
    assert result.returncode == 0, result.stderr
    assert "public preflight: ok" in result.stdout


@pytest.mark.parametrize("var,default", sorted(preflight_table().items()))
def test_preflight_rejects_each_local_default(var, default):
    env = {**PUBLIC_ENV, "KIONGA_SSO_LDAP": "true", "DEX_LDAP_BIND_PASSWORD": "strong-bind", var: default}
    result = run_preflight(env)
    assert result.returncode == 1
    assert f"{var} uses a built-in local default" in result.stderr


def test_preflight_rejects_a_default_reused_under_another_name():
    result = run_preflight({**PUBLIC_ENV, "LANGFUSE_SALT": "kionga-local-admin"})
    assert result.returncode == 1 and "LANGFUSE_SALT uses a built-in local default" in result.stderr


@pytest.mark.parametrize("var", ["LANGFUSE_INIT_USER_PASSWORD", "KIONGA_DOMAIN", "POSTGRES_PASSWORD"])
def test_preflight_rejects_a_missing_value(var):
    env = {k: v for k, v in PUBLIC_ENV.items() if k != var}
    result = run_preflight(env)
    assert result.returncode == 1 and f"{var} is not set" in result.stderr


def test_preflight_requires_the_ldap_bind_password_only_with_ldap():
    assert run_preflight(PUBLIC_ENV).returncode == 0
    result = run_preflight({**PUBLIC_ENV, "KIONGA_SSO_LDAP": "true"})
    assert result.returncode == 1 and "DEX_LDAP_BIND_PASSWORD is not set" in result.stderr


def test_preflight_treats_values_as_data():
    # Values are compared, never evaluated.
    result = run_preflight({**PUBLIC_ENV, "LANGFUSE_SALT": "$(touch /tmp/kionga-preflight-pwned) `id` ' \""})
    assert result.returncode == 0, result.stderr
    assert not os.path.exists("/tmp/kionga-preflight-pwned")


@pytest.mark.skipif(not shutil.which("bash"), reason="bash is required")
def test_public_up_refuses_a_local_default(tmp_path):
    # public-up.sh sources .env from its own directory, so run a copy.
    shutil.copy(ROOT / "public-up.sh", tmp_path / "public-up.sh")
    (tmp_path / "scripts").mkdir()
    shutil.copy(PREFLIGHT, tmp_path / "scripts" / "public-preflight.sh")
    env = {**PUBLIC_ENV, "LANGFUSE_INIT_USER_PASSWORD": "kionga-local-admin"}
    (tmp_path / ".env").write_text("".join(f"{k}='{v}'\n" for k, v in env.items()))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\necho docker-called >&2\nexit 0\n")
    (bin_dir / "docker").chmod(0o755)
    result = subprocess.run(
        ["bash", str(tmp_path / "public-up.sh")], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "KIONGA_LEGACY_PUBLIC_DEPLOY": "1"})
    assert result.returncode != 0
    assert "LANGFUSE_INIT_USER_PASSWORD uses a built-in local default" in result.stderr
    assert "docker-called" not in result.stderr


def test_preflight_runs_in_the_caddy_image(public):
    image = public["public-preflight"]["image"]
    if not shutil.which("docker") or subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        pytest.skip(f"{image} is not pulled")
    args = ["docker", "run", "--rm", "-v", f"{PREFLIGHT}:/kionga/public-preflight.sh:ro", "--entrypoint", "/bin/sh"]
    for key, value in {**PUBLIC_ENV, "JUPYTER_TOKEN": "kionga-local"}.items():
        args += ["-e", f"{key}={value}"]
    result = subprocess.run([*args, image, "/kionga/public-preflight.sh"], capture_output=True, text=True)
    assert result.returncode == 1 and "JUPYTER_TOKEN uses a built-in local default" in result.stderr


def test_env_example_lists_every_public_credential():
    example = (ROOT / ".env.example").read_text()
    for var in preflight_table():
        assert re.search(rf"^#?{var}=", example, re.M), f"{var} is required in public mode but missing from .env.example"


def test_local_stack_accepts_generated_database_credentials():
    # The preflight is public-only; local mode injects generated credentials.
    local = compose_config("compose.yaml")
    assert "public-preflight" not in local
    assert local["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith("test-")
    assert local["mlflow"]["environment"]["AWS_SECRET_ACCESS_KEY"] == local["objectstore"]["environment"]["RUSTFS_SECRET_KEY"]
