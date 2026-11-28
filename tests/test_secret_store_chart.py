"""Helm: OpenBao as the External Secrets Operator store, the gateway's egress to
OpenBao, Secret-driven rollouts, the workload secret example, and no HashiCorp
Vault anywhere. Renders with `helm template` and asserts on the objects."""

import re
import subprocess

import pytest
import yaml

from test_kubernetes_runtime_chart import find, render_raw
from test_production_deploy import ROOT, render_chart

BOOTSTRAP = (ROOT / "scripts/openbao-bootstrap.sh").read_text()


def bootstrap_paths(name):
    match = re.search(rf"^{name}=\((.*)\)$", BOOTSTRAP, re.M)
    return [p.strip('"') for p in match.group(1).split()]


def of_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def openbao(**overrides):
    values = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())["secretStore"]["openbao"]
    values.update(overrides)
    return {"openbao": values}


# The store ----------------------------------------------------------------------

def test_production_example_defaults_to_openbao(tmp_path):
    docs = render_chart(tmp_path)
    store = find(docs, "SecretStore", "kionga-store", "kionga-system")
    vault = store["spec"]["provider"]["vault"]
    assert vault["server"] == "https://openbao.openbao.svc:8200"
    assert vault["path"] == "kionga" and vault["version"] == "v2"
    auth = vault["auth"]["kubernetes"]
    assert auth == {"mountPath": "kubernetes", "role": "kionga-platform",
                    "serviceAccountRef": {"name": "kionga-openbao-reader", "audiences": ["openbao"]}}
    reader = find(docs, "ServiceAccount", "kionga-openbao-reader", "kionga-system")
    assert reader["automountServiceAccountToken"] is False
    workspace = find(docs, "SecretStore", "kionga-store", "kionga-workloads")
    assert workspace["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"] == "kionga-workspace"
    find(docs, "ServiceAccount", "kionga-openbao-reader", "kionga-workloads")
    for external in of_kind(docs, "ExternalSecret"):
        assert external["spec"]["secretStoreRef"] == {"name": "kionga-store", "kind": "SecretStore"}
        assert external["spec"]["refreshInterval"] == "1h"
    assert not any(d["kind"] == "Secret" for d in docs), "the chart never renders a Secret"


def test_production_example_reads_only_paths_the_platform_role_may_read():
    example = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())
    platform = set(bootstrap_paths("platform_paths"))
    for target, items in example["externalSecrets"]["targets"].items():
        for item in items:
            assert item["remoteRef"]["key"] in platform, f"{target} reads {item['remoteRef']['key']}, outside the platform policy"
            assert item["remoteRef"]["property"] == item["secretKey"]


def test_release_name_flows_into_roles_and_the_reader(tmp_path):
    values = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())
    values["images"] = {name: f"registry/{name}@sha256:" + "a" * 64 for name in values["images"]}
    file = tmp_path / "values.yaml"
    file.write_text(yaml.safe_dump(values))
    from test_production_deploy import helm_binary
    result = subprocess.run([helm_binary(), "template", "acme", str(ROOT / "helm/kionga"), "--namespace", "acme-system", "-f", str(file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    auth = find(docs, "SecretStore", "kionga-store", "acme-system")["spec"]["provider"]["vault"]["auth"]["kubernetes"]
    assert auth["role"] == "acme-platform" and auth["serviceAccountRef"]["name"] == "acme-openbao-reader"
    assert 'platform_sa="$release-openbao-reader"' in BOOTSTRAP and 'gateway_sa="$release-gateway"' in BOOTSTRAP


def test_ca_bundle_becomes_a_configmap_in_each_store_namespace(tmp_path):
    pem = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
    docs = render_chart(tmp_path, secretStore=openbao(caBundle=pem))
    for namespace in ("kionga-system", "kionga-workloads"):
        config = find(docs, "ConfigMap", "kionga-openbao-ca", namespace)
        assert config["data"]["ca.crt"] == pem
        store = find(docs, "SecretStore", "kionga-store", namespace)
        assert store["spec"]["provider"]["vault"]["caProvider"] == {"type": "ConfigMap", "name": "kionga-openbao-ca", "key": "ca.crt"}
    plain = render_chart(tmp_path)
    assert "caProvider" not in find(plain, "SecretStore", "kionga-store", "kionga-system")["spec"]["provider"]["vault"]
    assert not [d for d in plain if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "kionga-openbao-ca"]


def test_store_off_renders_nothing_for_openbao(tmp_path):
    docs = render_chart(tmp_path, secretStore=openbao(enabled=False))
    assert not of_kind(docs, "SecretStore")
    assert not [d for d in docs if d["metadata"]["name"] == "kionga-openbao-reader"]
    assert of_kind(docs, "ExternalSecret"), "a cloud store still serves the ExternalSecrets"


def test_workspace_store_is_optional(tmp_path):
    docs = render_chart(tmp_path, secretStore=openbao(workspace={"enabled": False, "storeName": "kionga-store", "role": ""}))
    assert [d["metadata"]["namespace"] for d in of_kind(docs, "SecretStore")] == ["kionga-system"]


@pytest.mark.parametrize("override,message", [
    ({"server": "http://openbao.openbao.svc:8200"}, "must be an https:// URL"),
])
def test_store_settings_are_validated(tmp_path, override, message):
    result = render_raw(tmp_path, secretStore=openbao(**override))
    assert result.returncode != 0 and message in result.stderr


def test_openbao_store_needs_a_namespaced_store_kind(tmp_path):
    example = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())["externalSecrets"]
    result = render_raw(tmp_path, externalSecrets=dict(example, storeKind="ClusterSecretStore"))
    assert result.returncode != 0 and "storeKind to SecretStore" in result.stderr


def test_refresh_interval_and_workspace_targets(tmp_path):
    example = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())["externalSecrets"]
    targets = {"kionga-model-storage": [{"secretKey": "AWS_ACCESS_KEY_ID", "remoteRef": {"key": "model-storage", "property": "AWS_ACCESS_KEY_ID"}}]}
    docs = render_chart(tmp_path, externalSecrets=dict(example, refreshInterval="15s", workspaceTargets=targets))
    model = find(docs, "ExternalSecret", "kionga-model-storage", "kionga-workloads")
    assert model["spec"]["secretStoreRef"] == {"name": "kionga-store", "kind": "SecretStore"}
    assert model["spec"]["target"]["name"] == "kionga-model-storage"
    assert all(e["spec"]["refreshInterval"] == "15s" for e in of_kind(docs, "ExternalSecret"))
    assert "model-storage" in bootstrap_paths("workspace_paths")


def test_preflight_requires_the_secretstore_api_when_openbao_is_on(tmp_path):
    versions = ["external-secrets.io/v1/ExternalSecret", "monitoring.coreos.com/v1/ServiceMonitor"]
    args = [arg for version in versions for arg in ("--api-versions", version)]
    result = render_raw(tmp_path, *args, preflight={"enabled": True, "enforceOffline": True})
    assert result.returncode != 0 and "external-secrets.io/v1/SecretStore" in result.stderr
    args += ["--api-versions", "external-secrets.io/v1/SecretStore"]
    assert render_raw(tmp_path, *args, preflight={"enabled": True, "enforceOffline": True}).returncode == 0


def test_preflight_checks_every_store_on_a_live_install():
    text = (ROOT / "helm/kionga/templates/preflight.yaml").read_text()
    # A live lookup of each store ExternalSecrets use, the workspace one
    # included: missing (when the chart does not render it) or not Ready fails
    # the install with the unseal command. scripts/kind-ci-up.sh runs it live.
    assert 'lookup "external-secrets.io/v1" $kind $namespace $name' in text
    assert "$o.workspace.storeName" in text
    assert "scripts/openbao-bootstrap.sh unseal" in text and "allowUnreadySecretStore" in text


def test_workspace_targets_need_the_workspace_store(tmp_path):
    example = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())["externalSecrets"]
    targets = {"kionga-model-storage": [{"secretKey": "A", "remoteRef": {"key": "model-storage", "property": "A"}}]}
    result = render_raw(tmp_path, externalSecrets=dict(example, workspaceTargets=targets),
                        secretStore=openbao(workspace={"enabled": False, "storeName": "kionga-store", "role": ""}))
    assert result.returncode != 0 and "secretStore.openbao.workspace.enabled" in result.stderr


# Gateway egress -------------------------------------------------------------------

def egress_policy(docs):
    return [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "kionga-gateway-openbao"]


def test_gateway_egress_to_openbao_only_when_enabled(tmp_path):
    assert not egress_policy(render_chart(tmp_path))
    docs = render_chart(tmp_path, secretStore=openbao(gatewayEgress={"enabled": True, "namespace": "openbao",
                                                                     "podLabels": {"app.kubernetes.io/name": "openbao"}, "port": 8200, "to": []}))
    [policy] = egress_policy(docs)
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/instance": "kionga", "app.kubernetes.io/component": "gateway"}
    assert policy["spec"]["policyTypes"] == ["Egress"]
    [rule] = policy["spec"]["egress"]
    assert rule["ports"] == [{"port": 8200, "protocol": "TCP"}]
    assert rule["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openbao"}},
                           "podSelector": {"matchLabels": {"app.kubernetes.io/name": "openbao"}}}]
    control = find(docs, "NetworkPolicy", "kionga-control")
    assert "8200" not in str(control["spec"]["egress"]), "only the gateway may reach OpenBao"


def test_gateway_egress_accepts_an_external_openbao(tmp_path):
    to = [{"ipBlock": {"cidr": "192.0.2.10/32"}}]
    docs = render_chart(tmp_path, secretStore=openbao(gatewayEgress={"enabled": True, "namespace": "openbao", "podLabels": {},
                                                                     "port": 8200, "to": to}))
    assert egress_policy(docs)[0]["spec"]["egress"][0]["to"] == to
    assert not egress_policy(render_chart(tmp_path, networkPolicy={"enabled": False},
                                          secretStore=openbao(gatewayEgress={"enabled": True, "namespace": "openbao", "podLabels": {},
                                                                             "port": 8200, "to": []})))


# Rollouts -------------------------------------------------------------------------

def reload_annotation(docs, component):
    deployment = find(docs, "Deployment", f"kionga-{component}")
    return (deployment["metadata"].get("annotations") or {}).get("secret.reloader.stakater.com/reload")


def test_deployments_name_the_secrets_they_mount_for_reloader(tmp_path):
    docs = render_chart(tmp_path, secretReferences={"secretName": "kionga-secret-references", "names": ["KIONGA_KB_SECRET_A"],
                                                    "knowledgeAllowlist": [], "connectionAllowlist": [], "featureAllowlist": [],
                                                    "storageAllowlist": [], "platformAllowlist": []})
    assert reload_annotation(docs, "gateway") == "kionga-gateway,kionga-secret-references"
    assert reload_annotation(docs, "feature-gateway") == "kionga-features"
    assert reload_annotation(docs, "storage-proxy") == "kionga-storage"
    assert reload_annotation(docs, "trace-proxy") == "kionga-tracing"
    assert reload_annotation(docs, "operator") is None, "the operator mounts no Secret"
    assert reload_annotation(render_chart(tmp_path), "gateway") == "kionga-gateway"


def test_secret_rollout_can_be_turned_off(tmp_path):
    docs = render_chart(tmp_path, secretRollout={"enabled": False})
    assert all(reload_annotation(docs, c) is None for c in ("gateway", "feature-gateway", "storage-proxy", "trace-proxy"))


# Examples -------------------------------------------------------------------------

def test_workload_secret_example_is_labelled_and_reads_only_workload_paths():
    [external] = [d for d in yaml.safe_load_all((ROOT / "helm/externalsecret-workload.example.yaml").read_text()) if d]
    assert external["kind"] == "ExternalSecret" and external["metadata"]["namespace"] == "kionga-workloads"
    assert external["spec"]["target"]["template"]["metadata"]["labels"] == {"kionga.dev/workload-secret": "true"}
    assert external["spec"]["secretStoreRef"] == {"name": "kionga-store", "kind": "SecretStore"}
    assert all(item["remoteRef"]["key"].startswith("workloads/") for item in external["spec"]["data"])
    assert "workloads/*" in bootstrap_paths("workspace_paths")


def test_openbao_store_example_matches_the_chart(tmp_path):
    docs = {d["kind"]: d for d in yaml.safe_load_all((ROOT / "helm/secretstore-openbao.example.yaml").read_text()) if d}
    rendered = find(render_chart(tmp_path), "SecretStore", "kionga-store", "kionga-system")
    example = docs["SecretStore"]["spec"]["provider"]["vault"]
    chart = rendered["spec"]["provider"]["vault"]
    assert example["auth"] == chart["auth"]
    assert {k: example[k] for k in ("server", "path", "version")} == {k: chart[k] for k in ("server", "path", "version")}
    assert docs["ServiceAccount"]["automountServiceAccountToken"] is False


def test_cloud_stores_stay_as_alternatives():
    for provider, key in (("aws", "aws"), ("gcp", "gcpsm"), ("azure", "azurekv")):
        docs = [d for d in yaml.safe_load_all((ROOT / f"helm/secretstore-{provider}.example.yaml").read_text()) if d]
        store = next(d for d in docs if d["kind"] == "SecretStore")
        assert key in store["spec"]["provider"] and store["metadata"]["name"] == "kionga-store"


# No HashiCorp Vault -----------------------------------------------------------------

def test_no_hashicorp_vault_images_charts_or_agent_config():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert "config/security/vault-auth.yaml" not in tracked
    banned = re.compile(r"hashicorp/vault|helm\.releases\.hashicorp\.com|vault-auth|vault-agent|vault\.hashicorp\.com", re.I)
    this = "tests/test_secret_store_chart.py"
    offenders = []
    for path in tracked:
        if path == this or not (ROOT / path).is_file():
            continue
        try:
            text = (ROOT / path).read_text()
        except UnicodeDecodeError:
            continue
        if banned.search(text):
            offenders.append(path)
    assert offenders == []
    assert "Vault security" not in (ROOT / "ARCHITECTURE.md").read_text()


# Transit for the credential key ------------------------------------------------------

def gateway_with_signing_key():
    components = yaml.safe_load((ROOT / "helm/kionga/values.yaml").read_text())["components"]
    gateway = dict(components["gateway"], secrets=["DATABASE_URL", "OIDC_CLIENT_SECRET", "KIONGA_INTERNAL_TOKEN", "KIONGA_SIGNING_KEY"])
    return {**components, "gateway": gateway}


def gateway_pod(docs):
    return find(docs, "Deployment", "kionga-gateway")["spec"]["template"]["spec"]


def test_transit_is_off_by_default(tmp_path):
    from test_kubernetes_runtime_chart import env
    assert "KIONGA_KEY_PROVIDER" not in env(render_chart(tmp_path), "gateway")


def test_transit_points_the_gateway_at_openbao_without_the_credential_key(tmp_path):
    from test_kubernetes_runtime_chart import env
    pem = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
    docs = render_chart(tmp_path, components=gateway_with_signing_key(),
                        secretStore=openbao(caBundle=pem, transit={"enabled": True, "address": "https://openbao-active.openbao.svc:8200",
                                                                    "key": "kionga-credential", "role": ""}))
    variables = env(docs, "gateway")
    assert variables["KIONGA_KEY_PROVIDER"] == "openbao"
    assert variables["OPENBAO_ADDR"] == "https://openbao-active.openbao.svc:8200"
    assert variables["OPENBAO_TRANSIT_KEY"] == "kionga-credential" and variables["OPENBAO_AUTH_ROLE"] == "kionga-gateway"
    assert variables["OPENBAO_CACERT_FILE"] == "/etc/kionga/openbao/ca.crt"
    assert variables["KIONGA_SIGNING_KEY_FILE"] == "/run/secrets/KIONGA_SIGNING_KEY"
    assert "KIONGA_CREDENTIAL_KEY_FILE" not in variables and "KIONGA_CREDENTIAL_KEY" not in variables
    pod = gateway_pod(docs)
    assert {"name": "openbao-ca", "configMap": {"name": "kionga-openbao-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}} in pod["volumes"]
    assert find(docs, "ServiceAccount", "kionga-gateway")["automountServiceAccountToken"] is True
    assert egress_policy(docs), "transit opens the gateway's egress to OpenBao"
    others = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] != "kionga-gateway"]
    assert all("OPENBAO_ADDR" not in str(d) for d in others)
    assert "auth/token/renew-self" in BOOTSTRAP and "derived=true" in BOOTSTRAP


def test_transit_needs_the_signing_key_and_https(tmp_path):
    result = render_raw(tmp_path, secretStore=openbao(transit={"enabled": True, "address": "https://openbao-active.openbao.svc:8200",
                                                                "key": "kionga-credential", "role": ""}))
    assert result.returncode != 0 and "KIONGA_SIGNING_KEY" in result.stderr
    result = render_raw(tmp_path, components=gateway_with_signing_key(),
                        secretStore=openbao(transit={"enabled": True, "address": "http://openbao:8200", "key": "k", "role": ""}))
    assert result.returncode != 0 and "transit.address must be an https://" in result.stderr



def test_spread_ignores_nodes_the_pods_cannot_run_on(tmp_path):
    # A rotated Secret rolls the gateway with one surge pod. With tainted nodes
    # counted as spread domains (the default), that pod could not schedule on a
    # cluster where every schedulable node already ran a replica.
    for deployment in (d for d in render_chart(tmp_path) if d["kind"] == "Deployment"):
        for constraint in deployment["spec"]["template"]["spec"]["topologySpreadConstraints"]:
            assert constraint["nodeTaintsPolicy"] == "Honor", deployment["metadata"]["name"]
