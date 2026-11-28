"""helm/prerequisites/install.sh installs pinned versions only, never
ingress-nginx, and KServe in Standard (raw) mode. Checked through --dry-run,
which prints the commands without running them."""

import re
import subprocess

import pytest
import yaml

from test_production_deploy import ROOT

SCRIPT = ROOT / "helm/prerequisites/install.sh"


def versions():
    pins = {}
    for line in (ROOT / "helm/prerequisites/versions.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            pins[key] = value
    return pins


def dry_run(*args):
    result = subprocess.run(["bash", str(SCRIPT), "--dry-run", *args], capture_output=True, text=True)
    return result


def commands(*args):
    result = dry_run(*args)
    assert result.returncode == 0, result.stderr
    return [line[2:] for line in result.stdout.splitlines() if line.startswith("+ ")]


def test_every_chart_is_pinned_to_versions_env():
    pins = versions()
    assert set(pins) == {"CERT_MANAGER_VERSION", "KEDA_VERSION", "KEDA_HTTP_ADDON_VERSION", "KSERVE_VERSION", "TRAEFIK_CHART_VERSION",
                         "OPENBAO_CHART_VERSION", "OPENBAO_VERSION", "OPENBAO_IMAGE_DIGEST", "EXTERNAL_SECRETS_VERSION",
                         "RELOADER_CHART_VERSION"}
    installs = [c for c in commands("--ingress", "traefik") if c.startswith("helm upgrade --install")]
    expected = {"traefik": pins["TRAEFIK_CHART_VERSION"], "cert-manager": pins["CERT_MANAGER_VERSION"], "keda": pins["KEDA_VERSION"],
                "keda-add-ons-http": pins["KEDA_HTTP_ADDON_VERSION"], "openbao": pins["OPENBAO_CHART_VERSION"],
                "external-secrets": pins["EXTERNAL_SECRETS_VERSION"], "reloader": pins["RELOADER_CHART_VERSION"]}
    for command in installs:
        release = command.split()[3]
        if release.startswith("kserve"):
            assert f"/releases/download/{pins['KSERVE_VERSION']}/" in command and pins["KSERVE_VERSION"] + ".tgz" in command
        else:
            assert f"--version {expected.pop(release)}" in command, command
    assert not expected, f"not installed: {expected}"
    digests = {k: v for k, v in pins.items() if k.endswith("_DIGEST")}
    assert all(re.fullmatch(r"v?\d+\.\d+\.\d+", v) for k, v in pins.items() if k not in digests), "pin exact versions"
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in digests.values()), "pin image digests"


def test_kserve_runs_in_standard_mode_without_its_own_ingress():
    kserve = next(c for c in commands() if c.startswith("helm upgrade --install kserve-resources"))
    assert "--set kserve.controller.deploymentMode=Standard" in kserve
    assert "--set kserve.controller.gateway.disableIngressCreation=true" in kserve
    runtimes = next(c for c in commands() if c.startswith("helm upgrade --install kserve-runtime-configs"))
    assert "kserve.servingruntime.enabled=true" in runtimes


def test_no_ingress_controller_by_default_and_never_ingress_nginx():
    default = commands()
    assert not any("traefik" in c for c in default)
    assert not any("ingress-nginx" in c for c in commands("--ingress", "traefik"))
    result = dry_run("--ingress", "ingress-nginx")
    assert result.returncode == 2 and "retired" in result.stderr


def test_traefik_allows_externalname_backends_and_is_not_the_default_class():
    traefik = next(c for c in commands("--ingress", "traefik") if c.startswith("helm upgrade --install traefik"))
    assert "providers.kubernetesIngress.allowExternalNameServices=true" in traefik
    assert "ingressClass.isDefaultClass=false" in traefik and "--namespace ingress-system" in traefik


def test_context_reaches_every_command():
    for command in commands("--kube-context", "prod", "--ingress", "traefik"):
        if command.startswith("helm upgrade"):
            assert "--kube-context prod" in command
        if command.startswith("kubectl"):
            assert "--context prod" in command


@pytest.mark.parametrize("name,release", [("cert-manager", "cert-manager"), ("keda", "keda"), ("keda-http", "keda-add-ons-http"),
                                          ("kserve", "kserve-crd"), ("openbao", "openbao"), ("external-secrets", "external-secrets"),
                                          ("reloader", "reloader")])
def test_components_can_be_skipped(name, release):
    assert not any(c.startswith(f"helm upgrade --install {release} ") for c in commands("--skip", name))


def test_unknown_arguments_are_refused():
    assert dry_run("--skip", "istio").returncode == 2
    assert dry_run("--bogus").returncode == 2


# Secrets: OpenBao, External Secrets Operator, Reloader --------------------------

def test_openbao_is_raft_ha_with_tls_pinned_by_digest_and_never_dev_mode():
    pins = versions()
    cmds = commands()
    openbao = next(c for c in cmds if c.startswith("helm upgrade --install openbao "))
    assert "openbao/openbao" in openbao and f"--version {pins['OPENBAO_CHART_VERSION']}" in openbao
    assert f"server.image.tag={pins['OPENBAO_VERSION']}@{pins['OPENBAO_IMAGE_DIGEST']}" in openbao
    assert "/openbao-values.yaml" in openbao
    # Sealed servers are never Ready, so the install must not wait for them.
    assert "--wait" not in openbao.split()
    tls = cmds.index(next(c for c in cmds if c.endswith("/openbao-tls.yaml")))
    assert cmds.index(next(c for c in cmds if "certificate/openbao-server-tls" in c)) > tls
    assert cmds.index(openbao) > tls
    cert_manager = cmds.index(next(c for c in cmds if c.startswith("helm upgrade --install cert-manager ")))
    assert tls > cert_manager

    values = yaml.safe_load((ROOT / "helm/prerequisites/openbao-values.yaml").read_text())
    server = values["server"]
    assert values["global"]["tlsDisable"] is False and values["injector"]["enabled"] is False
    assert server["ha"]["enabled"] and server["ha"]["replicas"] == 3 and server["ha"]["raft"]["enabled"]
    assert not server.get("dev", {}).get("enabled") and not server.get("standalone", {}).get("enabled")
    assert server["image"] == {"registry": "quay.io", "repository": "openbao/openbao"}
    assert server["podManagementPolicy"] == "Parallel"
    config = server["ha"]["raft"]["config"]
    assert 'storage "raft"' in config and "tls_disable" not in config
    assert config.count("retry_join") == 3 and config.count("leader_ca_cert_file") == 3
    assert {"name": "tls", "mountPath": "/openbao/tls", "readOnly": True} in server["volumeMounts"]
    assert server["volumes"][0]["secret"]["secretName"] == "openbao-server-tls"


def test_openbao_certificate_covers_every_address_clients_and_peers_use():
    docs = [d for d in yaml.safe_load_all((ROOT / "helm/prerequisites/openbao-tls.yaml").read_text()) if d]
    server = next(d for d in docs if d["kind"] == "Certificate" and d["metadata"]["name"] == "openbao-server-tls")
    names = set(server["spec"]["dnsNames"])
    assert {"openbao.openbao.svc", "*.openbao-internal", "localhost"} <= names
    assert server["spec"]["ipAddresses"] == ["127.0.0.1"]
    assert server["spec"]["issuerRef"]["name"] == "openbao-ca"
    assert docs[0] == {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "openbao"}}
    assert all(d["metadata"]["namespace"] == "openbao" for d in docs[1:])


def test_external_secrets_installs_its_crds_and_reloader_is_scoped_to_the_release_namespace():
    cmds = commands("--kionga-namespace", "acme-system")
    eso = next(c for c in cmds if c.startswith("helm upgrade --install external-secrets "))
    assert "external-secrets/external-secrets" in eso and "installCRDs=true" in eso and "--wait" in eso.split()
    reloader = next(c for c in cmds if c.startswith("helm upgrade --install reloader "))
    assert "reloader.watchGlobally=false" in reloader and "reloader.namespaces={acme-system}" in reloader
    assert "reloader.reloadStrategy=annotations" in reloader
    # The Role Reloader creates needs the release namespace to exist first.
    assert cmds.index("kubectl apply namespace acme-system") < cmds.index(reloader)
    assert dry_run("--kionga-namespace", "Bad_Name").returncode == 2


def test_extra_openbao_values_come_after_the_defaults(tmp_path):
    extra = tmp_path / "kind.yaml"
    extra.write_text("server: {}\n")
    openbao = next(c for c in commands("--openbao-values", str(extra)) if c.startswith("helm upgrade --install openbao "))
    assert openbao.index("openbao-values.yaml") < openbao.index(str(extra))
    assert dry_run("--openbao-values", str(tmp_path / "missing.yaml")).returncode == 2
