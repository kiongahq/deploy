"""helm/prerequisites/install.sh installs pinned versions only, never
ingress-nginx, and KServe in Standard (raw) mode. Checked through --dry-run,
which prints the commands without running them."""

import re
import subprocess

import pytest

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
    assert set(pins) == {"CERT_MANAGER_VERSION", "KEDA_VERSION", "KEDA_HTTP_ADDON_VERSION", "KSERVE_VERSION", "TRAEFIK_CHART_VERSION"}
    installs = [c for c in commands("--ingress", "traefik") if c.startswith("helm upgrade --install")]
    expected = {"traefik": pins["TRAEFIK_CHART_VERSION"], "cert-manager": pins["CERT_MANAGER_VERSION"], "keda": pins["KEDA_VERSION"],
                "keda-add-ons-http": pins["KEDA_HTTP_ADDON_VERSION"]}
    for command in installs:
        release = command.split()[3]
        if release.startswith("kserve"):
            assert f"/releases/download/{pins['KSERVE_VERSION']}/" in command and pins["KSERVE_VERSION"] + ".tgz" in command
        else:
            assert f"--version {expected.pop(release)}" in command, command
    assert not expected, f"not installed: {expected}"
    assert all(re.fullmatch(r"v?\d+\.\d+\.\d+", value) for value in pins.values()), "pin exact versions"


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
                                          ("kserve", "kserve-crd")])
def test_components_can_be_skipped(name, release):
    assert not any(c.startswith(f"helm upgrade --install {release} ") for c in commands("--skip", name))


def test_unknown_arguments_are_refused():
    assert dry_run("--skip", "istio").returncode == 2
    assert dry_run("--bogus").returncode == 2
