"""The sandbox manager holds engine access for code a model wrote, so its wiring is
a security boundary: internal network only, a generated token, gVisor or nothing,
and an unsafe flag that cannot reach a public or Kubernetes path."""

import hashlib
import os
import re
import shutil
import subprocess

import pytest
import yaml

from test_production_deploy import ROOT, render_chart
from test_public_preflight import run_preflight
from test_sso import PUBLIC_ENV, compose_config

UNSAFE = "KIONGA_SANDBOX_ALLOW_UNSAFE_RUNTIME"
SOCKET = "/var/run/docker.sock"
MANAGER = "kionga-sandbox-manager"
NAMESPACE = "kionga-sandboxes"
DIGEST = "@sha256:" + "a" * 64


def enabled(**overrides):
    return {"enabled": True, "replicas": 2, "image": "registry/sandbox-manager" + DIGEST, "namespace": NAMESPACE,
            "secretName": "kionga-sandbox", "runtimeClassName": "gvisor", "createRuntimeClass": False,
            "runtimeClassHandler": "runsc", "images": ["registry/sandbox" + DIGEST], "gitHosts": ["github.com"],
            "ttlSeconds": 86400, "kubernetesApiCidrs": ["192.0.2.10/32"], "internalCidrs": ["198.51.100.0/24"],
            "resources": {}, **overrides}


def find(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def manager_env(docs):
    container = find(docs, "Deployment", MANAGER)["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item.get("value") for item in container["env"]}


@pytest.fixture(scope="module")
def local():
    return compose_config("compose.yaml")


@pytest.fixture(scope="module")
def public():
    return compose_config("compose.yaml", "compose.public.yaml", env=PUBLIC_ENV)


# Compose: the local stack


def test_only_the_engine_control_services_mount_the_socket(local, public):
    for services in (local, public):
        mounted = {name for name, service in services.items()
                   if any(v.get("target") == SOCKET for v in service.get("volumes", []))}
        assert mounted == {"pipeline-runner", "serving-manager", "sandbox-manager"}


def test_manager_is_on_an_internal_network_with_no_published_port(local):
    manager = local["sandbox-manager"]
    assert set(manager["networks"]) == {"sandbox"}
    assert not manager.get("ports")
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["networks"]["sandbox"]["internal"] is True
    members = {name for name, service in local.items() if "sandbox" in (service.get("networks") or {})}
    assert members == {"gateway", "sandbox-manager"}, "only the gateway may reach the manager"


def test_gateway_stays_on_the_default_network_and_does_not_wait_for_the_manager(local):
    assert set(local["gateway"]["networks"]) == {"default", "sandbox"}
    assert "sandbox-manager" not in local["gateway"].get("depends_on", {})


def test_gateway_and_manager_share_the_token_and_the_agreed_address():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    gateway, manager = services["gateway"]["environment"], services["sandbox-manager"]["environment"]
    assert gateway["KIONGA_SANDBOX_URL"] == "http://sandbox-manager:%s" % manager["PORT"]
    assert gateway["KIONGA_SANDBOX_TOKEN"] == manager["KIONGA_SANDBOX_TOKEN"] == "${KIONGA_SANDBOX_TOKEN:-}"


def test_manager_is_hardened_apart_from_the_socket(local):
    manager = local["sandbox-manager"]
    assert manager["read_only"] is True and manager["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in manager["security_opt"]
    assert "privileged" not in manager


def test_local_default_is_gvisor_and_never_the_unsafe_flag(local):
    env = local["sandbox-manager"]["environment"]
    assert env["KIONGA_SANDBOX_RUNTIME"] == "runsc"
    assert env[UNSAFE] == ""
    assert env["KIONGA_SANDBOX_DRIVER"] == "docker"


def test_unsafe_opt_in_is_documented_and_commented_out():
    example = (ROOT / ".env.example").read_text().splitlines()
    assert f"#{UNSAFE}=1" in example
    assert not any(line.startswith(UNSAFE) for line in example)
    assert f"{UNSAFE}=1" in (ROOT / "README.md").read_text()


def test_generated_local_secrets_never_turn_the_flag_on():
    for script in ("scripts/local-up.sh", "scripts/container.sh", "Makefile"):
        assert UNSAFE not in (ROOT / script).read_text(), script


# Compose: public mode


def test_public_overlay_pins_gvisor_whatever_env_says():
    env = {**PUBLIC_ENV, UNSAFE: "1", "KIONGA_SANDBOX_RUNTIME": "runc", "KIONGA_ENVIRONMENT": "development"}
    manager = compose_config("compose.yaml", "compose.public.yaml", env=env)["sandbox-manager"]["environment"]
    assert manager["KIONGA_SANDBOX_RUNTIME"] == "runsc"
    assert manager[UNSAFE] == ""
    assert manager["KIONGA_ENVIRONMENT"] == "production"


def test_public_manager_waits_for_the_preflight(public):
    condition = public["sandbox-manager"]["depends_on"]["public-preflight"]["condition"]
    assert condition == "service_completed_successfully"
    assert not public["sandbox-manager"].get("ports")


def test_preflight_service_sees_the_values_it_must_refuse():
    overlay = (ROOT / "compose.public.yaml").read_text()
    service = overlay[overlay.index("public-preflight:"):overlay.index("  caddy:")]
    for name in (UNSAFE, "KIONGA_SANDBOX_RUNTIME"):
        assert "%s: ${%s:-}" % (name, name) in service
    for name in ("KIONGA_SANDBOX_TOKEN", "KIONGA_SANDBOX_IMAGES"):
        assert "%s: ${%s:?" % (name, name) in service


@pytest.mark.parametrize("var", ["KIONGA_SANDBOX_TOKEN", "KIONGA_SANDBOX_IMAGES"])
def test_public_overlay_and_preflight_require(var):
    env = {k: v for k, v in PUBLIC_ENV.items() if k != var}
    with pytest.raises(AssertionError, match=var):
        compose_config("compose.yaml", "compose.public.yaml", env=env)
    result = run_preflight(env)
    assert result.returncode == 1 and f"{var} is not set" in result.stderr


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_preflight_refuses_the_unsafe_flag(value):
    result = run_preflight({**PUBLIC_ENV, UNSAFE: value})
    assert result.returncode == 1 and UNSAFE in result.stderr


@pytest.mark.parametrize("runtime", ["runc", "crun", "RUNSC"])
def test_preflight_refuses_another_runtime(runtime):
    result = run_preflight({**PUBLIC_ENV, "KIONGA_SANDBOX_RUNTIME": runtime})
    assert result.returncode == 1 and "must be runsc" in result.stderr


def test_preflight_accepts_explicit_safe_values():
    env = {**PUBLIC_ENV, UNSAFE: "0", "KIONGA_SANDBOX_RUNTIME": "runsc"}
    assert run_preflight(env).returncode == 0


def test_preflight_refuses_a_short_token():
    result = run_preflight({**PUBLIC_ENV, "KIONGA_SANDBOX_TOKEN": "short"})
    assert result.returncode == 1 and "at least 32" in result.stderr and "short" not in result.stderr


@pytest.mark.parametrize("images", ["registry/sandbox:latest", "registry/sandbox@sha256:abc",
                                    "registry/sandbox" + DIGEST + ",registry/other:1"])
def test_preflight_refuses_an_image_without_a_digest(images):
    result = run_preflight({**PUBLIC_ENV, "KIONGA_SANDBOX_IMAGES": images})
    assert result.returncode == 1 and "repository@sha256:digest" in result.stderr


def test_preflight_accepts_several_pinned_images():
    images = ",".join(["registry/a" + DIGEST, "registry/b" + DIGEST])
    assert run_preflight({**PUBLIC_ENV, "KIONGA_SANDBOX_IMAGES": images}).returncode == 0


def public_up(tmp_path, stub_run):
    shutil.copy(ROOT / "public-up.sh", tmp_path / "public-up.sh")
    (tmp_path / "scripts").mkdir()
    for name in ("public-preflight.sh", "check-sandbox-runtime.sh", "container.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    keys = {name: os.environ[name] for name in ("TRACE_PROXY_PROJECT_KEY", "TRACE_PROXY_KEYS",
                                                "SERVING_OBJECTSTORE_ACCESS_KEY", "SERVING_OBJECTSTORE_SECRET_KEY")}
    (tmp_path / ".env").write_text("".join(f"{k}='{v}'\n" for k, v in {**PUBLIC_ENV, **keys}.items()))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/bin/sh
if [ "$1" = info ]; then echo '{{"runsc":{{"path":"/usr/bin/runsc"}}}}'; exit 0; fi
if [ "$1" = image ]; then exit 0; fi
if [ "$1" = run ]; then {stub_run}; fi
echo "docker-called $1" >&2
exit 1
""")
    stub.chmod(0o755)
    return subprocess.run(["bash", str(tmp_path / "public-up.sh")], capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "KIONGA_LEGACY_PUBLIC_DEPLOY": "1"})


def test_public_up_stops_when_the_probe_fails_even_if_the_engine_lists_runsc(tmp_path):
    # Podman lists runsc when it is not installed, so the list must not count.
    result = public_up(tmp_path, "echo 'runsc: executable file not found'; exit 125")
    assert result.returncode != 0
    assert "failed to start under runsc" in result.stderr
    assert "docker-called compose" not in result.stderr


def test_public_up_stops_when_the_probe_runs_a_normal_kernel(tmp_path):
    result = public_up(tmp_path, "echo 'Linux version 6.8.0'; exit 0")
    assert result.returncode != 0
    assert "does not mention gVisor" in result.stderr
    assert "docker-called compose" not in result.stderr


def test_public_up_continues_after_a_real_gvisor_container(tmp_path):
    result = public_up(tmp_path, "echo '[0.0] Starting gVisor...'; exit 0")
    assert "A container ran under gVisor" in result.stdout
    assert "docker-called compose" in result.stderr


def test_production_vm_stack_has_no_manager_socket_or_flag():
    text = (ROOT / "vm/compose.yaml").read_text()
    services = yaml.safe_load(text)["services"]
    assert "sandbox-manager" not in services
    assert "docker.sock" not in text and UNSAFE not in text
    assert UNSAFE not in (ROOT / "vm/up.py").read_text()


# Helm


def test_chart_renders_nothing_for_sandboxes_by_default(tmp_path):
    docs = render_chart(tmp_path)
    assert not any("sandbox" in d["metadata"]["name"] for d in docs)
    gateway = find(docs, "Deployment", "kionga-gateway")["spec"]["template"]["spec"]["containers"][0]
    assert not any(item["name"].startswith("KIONGA_SANDBOX") for item in gateway["env"])


def test_unsafe_flag_cannot_reach_the_chart(tmp_path):
    for path in (ROOT / "helm").rglob("*.yaml"):
        assert UNSAFE not in path.read_text(), path
    docs = render_chart(tmp_path, sandbox=enabled())
    assert UNSAFE not in yaml.safe_dump_all(docs)
    env = manager_env(docs)
    assert env["KIONGA_ENVIRONMENT"] == "production"
    assert env["KIONGA_SANDBOX_DRIVER"] == "kubernetes"
    assert env["KIONGA_SANDBOX_RUNTIME"] == "gvisor"
    assert env["KIONGA_SANDBOX_NAMESPACE"] == NAMESPACE


def test_manager_takes_no_shared_config_or_extra_env(tmp_path):
    # config.json and extraEnv are how other components receive settings.
    pod = find(render_chart(tmp_path, sandbox=enabled()), "Deployment", MANAGER)["spec"]["template"]["spec"]
    assert [volume["name"] for volume in pod["volumes"]] == ["tmp"]
    assert "extraEnv" not in (ROOT / "helm/kionga/templates/sandbox-manager.yaml").read_text()


@pytest.mark.parametrize("overrides,message", [
    ({"allowUnsafeRuntime": True}, "local development only"),
    ({"runtimeClassName": "runc"}, "gVisor RuntimeClass"),
    ({"runtimeClassName": ""}, "gVisor RuntimeClass"),
    ({"image": "registry/sandbox-manager:latest"}, "sandbox.image must"),
    ({"images": []}, "at least one"),
    ({"images": ["registry/sandbox:latest"]}, "immutable"),
    ({"replicas": 1}, "at least 2"),
    ({"namespace": "kionga-system"}, "must differ"),
    ({"namespace": "kionga-workloads"}, "must differ"),
    ({"kubernetesApiCidrs": []}, "kubernetesApiCidrs is required"),
    ({"internalCidrs": ["fd00::/8"]}, "IPv4"),
    ({"secretName": ""}, "secretName is required"),
])
def test_chart_refuses(tmp_path, overrides, message):
    with pytest.raises(AssertionError, match=message):
        render_chart(tmp_path, sandbox=enabled(**overrides))


def test_rbac_stops_at_the_sandbox_namespace(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    role = find(docs, "Role", MANAGER)
    assert role["metadata"]["namespace"] == NAMESPACE
    resources = {r: rule["verbs"] for rule in role["rules"] for r in rule["resources"]}
    assert resources == {"pods": ["get", "list", "create", "delete"], "pods/exec": ["create", "get"],
                         "persistentvolumeclaims": ["get", "list", "create", "delete"], "networkpolicies": ["get"]}
    binding = find(docs, "RoleBinding", MANAGER)
    assert binding["metadata"]["namespace"] == NAMESPACE
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": MANAGER, "namespace": "kionga-system"}]


def test_the_only_cluster_grant_is_reading_one_runtime_class(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    name = "kionga-system-kionga-sandbox-manager-runtimeclass"
    assert find(docs, "ClusterRole", name)["rules"] == [
        {"apiGroups": ["node.k8s.io"], "resources": ["runtimeclasses"], "resourceNames": ["gvisor"], "verbs": ["get"]}]
    binding = find(docs, "ClusterRoleBinding", name)
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": MANAGER, "namespace": "kionga-system"}]
    baseline = render_chart(tmp_path)
    new = [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding") and d not in baseline]
    assert {d["metadata"]["name"] for d in new} == {name}


def test_sandbox_namespace_is_default_deny_even_with_network_policies_off(tmp_path):
    for network_policy in ({"enabled": True, "externalEgress": []}, {"enabled": False, "externalEgress": []}):
        docs = render_chart(tmp_path, sandbox=enabled(), networkPolicy=network_policy)
        policy = find(docs, "NetworkPolicy", "kionga-sandbox-default-deny")
        assert policy["metadata"]["namespace"] == NAMESPACE
        assert policy["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
        namespace = find(docs, "Namespace", NAMESPACE)
        assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_policy_name_matches_the_prefix_the_manager_looks_up(tmp_path):
    # The driver reads <prefix>default-deny and, with get only, fails closed without it.
    docs = render_chart(tmp_path, sandbox=enabled())
    prefix = manager_env(docs)["KIONGA_SANDBOX_NAME_PREFIX"]
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,30}", prefix), "the manager refuses any other prefix"
    assert find(docs, "NetworkPolicy", prefix + "default-deny")["metadata"]["namespace"] == NAMESPACE


def test_default_deny_covers_every_role_the_driver_creates(tmp_path):
    policy = find(render_chart(tmp_path, sandbox=enabled()), "NetworkPolicy", "kionga-sandbox-default-deny")
    assert policy["spec"]["podSelector"] == {}, "broader than the driver's sandbox and probe selector, never narrower"


def test_sandbox_pods_have_no_allowance_only_labelled_git_helpers_do(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    policies = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"].get("namespace") == NAMESPACE]
    allowing = [d for d in policies if d["spec"].get("egress") or d["spec"].get("ingress")]
    assert [d["metadata"]["name"] for d in allowing] == ["kionga-sandbox-git-helper"]
    assert allowing[0]["spec"]["podSelector"] == {"matchLabels": {"kionga.dev/sandbox-role": "git-helper"}}
    assert "ingress" not in allowing[0]["spec"]


def test_git_helper_cannot_reach_internal_ranges_metadata_or_the_api_server(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    dns, web = find(docs, "NetworkPolicy", "kionga-sandbox-git-helper")["spec"]["egress"]
    assert {port["port"] for port in dns["ports"]} == {53}
    assert dns["to"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "kube-system"}
    assert web["ports"] == [{"port": 443, "protocol": "TCP"}]
    block = web["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
            "127.0.0.0/8", "192.0.2.10/32", "198.51.100.0/24"} <= set(block["except"])


def test_no_git_hosts_means_no_git_egress(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled(gitHosts=[]))
    assert not any(d["metadata"]["name"] == "kionga-sandbox-git-helper" for d in docs)


def test_manager_reaches_only_dns_and_the_api_server_and_only_the_gateway_reaches_it(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    policy = find(docs, "NetworkPolicy", MANAGER)["spec"]
    [ingress] = policy["ingress"]
    assert ingress["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"
    assert ingress["ports"] == [{"port": 8088}]
    dns, api = policy["egress"]
    assert {port["port"] for port in dns["ports"]} == {53}
    assert api["to"] == [{"ipBlock": {"cidr": "192.0.2.10/32"}}]
    control = find(docs, "NetworkPolicy", "kionga-control")["spec"]
    assert any(rule.get("ports") == [{"port": 8088}] for rule in control["egress"])


def test_manager_pod_is_hardened_and_outside_the_control_policy(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled())
    deployment = find(docs, "Deployment", MANAGER)
    assert "app.kubernetes.io/instance" not in deployment["spec"]["template"]["metadata"]["labels"]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}}
    assert "volumeMounts" in container and not any("sock" in m["mountPath"] for m in container["volumeMounts"])


def test_gateway_and_manager_read_the_token_from_the_secret(tmp_path):
    docs = render_chart(tmp_path, sandbox=enabled(secretName="sandbox-token"))
    reference = {"secretKeyRef": {"name": "sandbox-token", "key": "KIONGA_SANDBOX_TOKEN"}}
    gateway = find(docs, "Deployment", "kionga-gateway")["spec"]["template"]["spec"]["containers"][0]["env"]
    manager = find(docs, "Deployment", MANAGER)["spec"]["template"]["spec"]["containers"][0]["env"]
    for env in (gateway, manager):
        assert next(item for item in env if item["name"] == "KIONGA_SANDBOX_TOKEN")["valueFrom"] == reference
    urls = {item["name"]: item.get("value") for item in gateway}
    assert urls["KIONGA_SANDBOX_URL"] == "http://kionga-sandbox-manager.kionga-system.svc:8088"


def test_runtime_class_is_optional_and_uses_the_handler(tmp_path):
    assert not [d for d in render_chart(tmp_path, sandbox=enabled()) if d["kind"] == "RuntimeClass"]
    [runtime] = [d for d in render_chart(tmp_path, sandbox=enabled(createRuntimeClass=True)) if d["kind"] == "RuntimeClass"]
    assert runtime["metadata"]["name"] == "gvisor" and runtime["handler"] == "runsc"


# Optional runtime install


def run_script(tmp_path, *args, uname="Linux", machine="x86_64", uid="1000", env=None):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in {
        "uname": f'#!/bin/sh\n[ "$1" = -m ] && echo {machine} || echo {uname}\n',
        "id": f"#!/bin/sh\necho {uid}\n",
        "docker": "#!/bin/sh\nexit 0\n",
        "bzip2": '#!/bin/sh\nexec /usr/bin/env -i PATH=/usr/bin:/bin bzip2 "$@"\n',
        # Serves the archive the test built; the URL is recorded for assertions.
        "curl": f"""#!/bin/sh
for arg in "$@"; do last="$arg"; done
echo "$last" >>"{tmp_path}/urls"
while [ $# -gt 0 ]; do [ "$1" = --output ] && cp "{tmp_path}/gvisor.tar.bz2" "$2"; shift; done
""",
        "systemctl": f'#!/bin/sh\necho "$@" >>"{tmp_path}/systemctl"\n',
    }.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    if not shutil.which("sha512sum", path="/usr/bin:/bin"):
        # macOS: the same check, for this test only.
        (stubs / "sha512sum").write_text("#!/bin/sh\nexec shasum -a 512 --check --quiet -\n")
        (stubs / "sha512sum").chmod(0o755)
    return subprocess.run(["bash", str(ROOT / "scripts/install-runsc.sh"), *args], capture_output=True, text=True,
                          env={"PATH": f"{stubs}:/usr/bin:/bin", **(env or {})})


def archive(tmp_path):
    """A release-shaped archive with a file that must stay behind, and its digest."""
    source = tmp_path / "release"
    (source / "gvisor-bin").mkdir(parents=True)
    (source / "gvisor-bin" / "gvisor_sentry").write_text("#!/bin/sh\n")
    (source / "LICENSE").write_text("not installed")
    (source / "containerd-shim-runsc-v1").write_text("#!/bin/sh\n")
    (source / "runsc").write_text(f'#!/bin/sh\necho "$@" >>"{tmp_path}/runsc-calls"\n')
    target = tmp_path / "gvisor.tar.bz2"
    subprocess.run(["tar", "-cjf", str(target), "-C", str(source), "runsc", "containerd-shim-runsc-v1", "gvisor-bin", "LICENSE"], check=True)
    return hashlib.sha512(target.read_bytes()).hexdigest()


def test_install_refuses_anything_but_linux(tmp_path):
    result = run_script(tmp_path, uname="Darwin")
    assert result.returncode == 1 and "Linux hosts only" in result.stderr
    assert not (tmp_path / "urls").exists()


def test_install_dry_run_names_the_pinned_archive_and_changes_nothing(tmp_path):
    result = run_script(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "release/20261026.0/x86_64/gvisor.tar.bz2" in result.stdout
    assert "26e4a327" in result.stdout and "448a" in result.stdout
    assert not (tmp_path / "urls").exists() and not (tmp_path / "systemctl").exists()


def test_each_architecture_has_its_own_full_pinned_digest(tmp_path):
    digests = set()
    for machine in ("x86_64", "aarch64"):
        result = run_script(tmp_path, "--dry-run", machine=machine)
        assert f"/{machine}/gvisor.tar.bz2" in result.stdout
        digests.add(re.search(r"SHA-512 ([0-9a-f]{128})\n", result.stdout).group(1))
    assert len(digests) == 2


def test_install_refuses_an_unknown_architecture(tmp_path):
    result = run_script(tmp_path, "--dry-run", machine="riscv64")
    assert result.returncode == 1 and "no build" in result.stderr


@pytest.mark.parametrize("release", ["latest", "../../evil"])
def test_install_refuses_a_release_that_is_not_a_date(tmp_path, release):
    result = run_script(tmp_path, "--dry-run", env={"KIONGA_RUNSC_RELEASE": release})
    assert result.returncode == 1 and "release date" in result.stderr


def test_another_release_needs_its_own_digest(tmp_path):
    result = run_script(tmp_path, "--dry-run", env={"KIONGA_RUNSC_RELEASE": "20260101.0"})
    assert result.returncode == 1 and "needs KIONGA_RUNSC_SHA512" in result.stderr


def test_install_requires_root_before_downloading(tmp_path):
    result = run_script(tmp_path)
    assert result.returncode == 1 and "run as root" in result.stderr
    assert not (tmp_path / "urls").exists()


@pytest.mark.skipif(not shutil.which("bzip2"), reason="bzip2 is required")
def test_install_puts_the_binaries_and_their_sidecars_together(tmp_path):
    digest = archive(tmp_path)
    bin_dir = tmp_path / "installed"
    bin_dir.mkdir()
    result = run_script(tmp_path, uid="0", env={"KIONGA_RUNSC_SHA512": digest, "KIONGA_RUNSC_BIN_DIR": str(bin_dir)})
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in bin_dir.iterdir()) == ["containerd-shim-runsc-v1", "gvisor-bin", "runsc"]
    assert os.access(bin_dir / "runsc", os.X_OK)
    # runsc looks for gvisor-bin/ beside itself, and every user may need to run it.
    assert (bin_dir / "gvisor-bin" / "gvisor_sentry").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "runsc-calls").read_text().strip() == "install"
    assert (tmp_path / "systemctl").read_text().strip() == "reload docker"
    assert (tmp_path / "urls").read_text().strip().endswith("/release/20261026.0/x86_64/gvisor.tar.bz2")


@pytest.mark.skipif(not shutil.which("bzip2"), reason="bzip2 is required")
def test_install_stops_on_a_checksum_mismatch(tmp_path):
    archive(tmp_path)
    bin_dir = tmp_path / "installed"
    bin_dir.mkdir()
    result = run_script(tmp_path, uid="0", env={"KIONGA_RUNSC_SHA512": "0" * 128, "KIONGA_RUNSC_BIN_DIR": str(bin_dir)})
    assert result.returncode == 1 and "checksum mismatch" in result.stderr
    assert not list(bin_dir.iterdir()) and not (tmp_path / "systemctl").exists()


def test_install_is_only_reached_through_its_own_target():
    makefile = (ROOT / "Makefile").read_text()
    assert re.findall(r"^(\S+):.*\n\t.*install-runsc", makefile, re.M) == ["sandbox-runtime-install"]
    for script in [*(ROOT / "scripts").glob("*.sh"), ROOT / "public-up.sh", ROOT / "vm/up.py"]:
        if script.name != "install-runsc.sh":
            assert "install-runsc.sh" not in script.read_text(), script.name
    assert "sandbox-runtime-install" in (ROOT / "README.md").read_text()


def probe(tmp_path, output="", status=0, local=True, pull="exit 0", **env):
    shutil.rmtree(tmp_path / "bin", ignore_errors=True)
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "docker"
    stub.write_text(f"""#!/bin/sh
echo "$@" >>"{tmp_path}/probe-args"
if [ "$1" = image ]; then exit {0 if local else 1}; fi
if [ "$1" = pull ]; then {pull}; fi
echo '{output}'
exit {status}
""")
    stub.chmod(0o755)
    return subprocess.run(["bash", str(ROOT / "scripts/check-sandbox-runtime.sh")], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{tmp_path}/bin:/usr/bin:/bin", "KIONGA_ENGINE": "docker", **env})


def test_verify_starts_a_container_with_no_network_under_runsc(tmp_path):
    assert probe(tmp_path, "[    0.000000] Starting gVisor...").returncode == 0
    run = (tmp_path / "probe-args").read_text().splitlines()[-1]
    assert run.startswith("run --rm --pull never --network none --runtime runsc ") and run.endswith(" dmesg")
    assert "info" not in (tmp_path / "probe-args").read_text().split(), "a runtime list proves nothing on Podman"


def test_probe_image_is_pinned_by_a_multi_architecture_digest(tmp_path):
    probe(tmp_path, "gVisor")
    run = (tmp_path / "probe-args").read_text().splitlines()[-1]
    assert re.search(r" busybox:1\.37@sha256:[a-f0-9]{64} dmesg$", run)


def test_verify_requires_the_gvisor_kernel_banner(tmp_path):
    fake = probe(tmp_path, "Linux version 6.8.0")
    assert fake.returncode == 1 and "does not mention gVisor" in fake.stderr
    failed = probe(tmp_path, "unknown runtime", 125)
    assert failed.returncode == 1 and "failed to start" in failed.stderr
    assert "A container ran" not in failed.stdout + fake.stdout


def test_verify_does_not_pull_an_image_it_already_has(tmp_path):
    probe(tmp_path, "gVisor")
    assert not any(line.startswith("pull ") for line in (tmp_path / "probe-args").read_text().splitlines())


def test_verify_pulls_a_missing_image_then_runs(tmp_path):
    assert probe(tmp_path, "gVisor", local=False).returncode == 0
    commands = [line.split()[0] for line in (tmp_path / "probe-args").read_text().splitlines()]
    assert commands == ["image", "pull", "run"]


def test_a_host_without_network_gets_a_message_and_not_a_hang(tmp_path):
    result = probe(tmp_path, local=False, pull="exec sleep 25", KIONGA_RUNSC_PROBE_PULL_SECONDS="1")
    assert result.returncode == 1
    assert "took longer than 1 seconds" in result.stderr and "no network" in result.stderr
    assert not any(line.startswith("run ") for line in (tmp_path / "probe-args").read_text().splitlines())


def test_a_failed_pull_gets_the_same_guidance(tmp_path):
    result = probe(tmp_path, local=False, pull="echo 'dial tcp: no route to host'; exit 1")
    assert result.returncode == 1
    assert "could not pull" in result.stderr and "no route to host" in result.stderr and "docker load" in result.stderr


def test_a_hung_probe_container_is_stopped(tmp_path):
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "docker"
    stub.write_text('#!/bin/sh\n[ "$1" = image ] && exit 0\nexec sleep 25\n')
    stub.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "scripts/check-sandbox-runtime.sh")], capture_output=True, text=True, timeout=20,
                            env={"PATH": f"{tmp_path}/bin:/usr/bin:/bin", "KIONGA_ENGINE": "docker", "KIONGA_RUNSC_PROBE_RUN_SECONDS": "1"})
    assert result.returncode == 1 and "did not finish within 1 seconds" in result.stderr


def test_manager_needs_only_a_small_tmp_and_passes_the_git_settings():
    manager = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["sandbox-manager"]
    assert manager["tmpfs"] == ["/tmp:size=16m"]
    for name in ("KIONGA_SANDBOX_HELPER_IMAGE", "KIONGA_SANDBOX_GIT_NETWORK"):
        assert manager["environment"][name] == "${%s:-}" % name


def test_empty_image_list_is_documented():
    assert "`KIONGA_SANDBOX_IMAGES` empty" in (ROOT / "README.md").read_text()
