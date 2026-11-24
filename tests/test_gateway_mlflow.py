"""The gateway reads a model's saved input example and signature from MLflow for
the console's Test dialog. It needs the MLflow server's address, the token as a
secret when MLflow requires one, and a network path to MLflow. It uses the
server's own artifact route, so it never receives object store credentials."""

import json
from urllib.parse import urlparse

import pytest
import yaml

from test_production_deploy import ROOT, render_chart

MLFLOW_EGRESS = {"to": [{"ipBlock": {"cidr": "10.0.50.0/24"}}], "ports": [{"port": 5000}]}
OBJECT_STORE_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_ACCESS_KEY", "S3_SECRET_KEY",
                      "MLFLOW_S3_ENDPOINT_URL", "OBJECTSTORE_ACCESS_KEY", "OBJECTSTORE_SECRET_KEY")


def load(path):
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if doc]


def find(docs, kind, name):
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert matches, f"{kind}/{name} not found"
    return matches[0]


def container(docs, name):
    return find(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][0]


def env(docs, name):
    return {item["name"]: item for item in container(docs, name)["env"]}


# Compose -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def services():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]


def test_compose_gateway_points_at_the_mlflow_service(services):
    address = urlparse(services["gateway"]["environment"]["MLFLOW_URL"])
    assert (address.scheme, address.hostname) == ("http", "mlflow")
    assert f"--port={address.port}" in " ".join(services["mlflow"]["command"])
    # The same server the pipelines and the serving manager log to and load from.
    assert services["serving-manager"]["environment"]["MLFLOW_TRACKING_URI"] == services["gateway"]["environment"]["MLFLOW_URL"]


def test_compose_gateway_gets_no_object_store_credentials(services):
    assert not set(OBJECT_STORE_NAMES) & set(services["gateway"]["environment"])


def test_compose_gateway_does_not_wait_for_mlflow(services):
    # The input example is optional; the gateway must start without MLflow.
    assert "mlflow" not in services["gateway"]["depends_on"]


def test_public_overlay_keeps_the_internal_mlflow_address():
    # The overlay uses Compose-only YAML tags, so it is checked as text: it must
    # not repoint the gateway at another MLflow.
    assert "MLFLOW_URL" not in (ROOT / "compose.public.yaml").read_text()


# Helm chart --------------------------------------------------------------------

def test_chart_gives_the_gateway_the_operators_mlflow_url(tmp_path):
    docs = render_chart(tmp_path)
    config = json.loads(find(docs, "ConfigMap", "kionga-config")["data"]["config.json"])
    assert config["MLFLOW_URL"] == "https://mlflow.internal.example.com"
    for component in ("gateway", "operator"):
        name = f"kionga-{component}"
        assert env(docs, name)["KIONGA_CONFIG_FILE"]["value"] == "/etc/kionga/config.json"
        assert {"name": "config", "mountPath": "/etc/kionga", "readOnly": True} in container(docs, name)["volumeMounts"]
        volumes = find(docs, "Deployment", name)["spec"]["template"]["spec"]["volumes"]
        assert {"name": "config", "configMap": {"name": "kionga-config"}} in volumes
        # The address is not also set on the container, where it would hide the shared file.
        assert "MLFLOW_URL" not in env(docs, name)


def test_chart_gateway_has_no_mlflow_token_or_object_store_credentials_by_default(tmp_path):
    names = set(env(render_chart(tmp_path), "kionga-gateway"))
    assert not {name for name in names if "MLFLOW_TOKEN" in name}
    assert not {name for name in names if name.removesuffix("_FILE") in OBJECT_STORE_NAMES}


@pytest.mark.parametrize("component,secret", [("gateway", "kionga-gateway"), ("operator", "kionga-operator")])
def test_chart_delivers_the_mlflow_token_as_a_secret_file(tmp_path, component, secret):
    defaults = yaml.safe_load((ROOT / "helm/kionga/values.yaml").read_text())["components"]
    components = {component: {"secrets": [*defaults[component]["secrets"], "MLFLOW_TOKEN"]}}
    docs = render_chart(tmp_path, components=components)
    name = f"kionga-{component}"
    variables = env(docs, name)
    assert variables["MLFLOW_TOKEN_FILE"] == {"name": "MLFLOW_TOKEN_FILE", "value": "/run/secrets/MLFLOW_TOKEN"}
    assert "MLFLOW_TOKEN" not in variables, "the token is a mounted file, never a literal"
    assert {"name": "secrets", "mountPath": "/run/secrets", "readOnly": True} in container(docs, name)["volumeMounts"]
    [volume] = [v for v in find(docs, "Deployment", name)["spec"]["template"]["spec"]["volumes"] if v["name"] == "secrets"]
    assert volume["secret"]["secretName"] == secret
    assert {"key": "MLFLOW_TOKEN", "path": "MLFLOW_TOKEN"} in volume["secret"]["items"]


def test_chart_never_puts_the_mlflow_token_in_the_config_file(tmp_path):
    config = json.loads(find(render_chart(tmp_path), "ConfigMap", "kionga-config")["data"]["config.json"])
    assert not [key for key in config if "TOKEN" in key and key != "OIDC_TOKEN_URL"]


def test_chart_egress_to_mlflow_covers_the_gateway_and_the_operator(tmp_path):
    docs = render_chart(tmp_path, networkPolicy={"enabled": True, "externalEgress": [MLFLOW_EGRESS]})
    control = find(docs, "NetworkPolicy", "kionga-control")["spec"]
    assert "Egress" in control["policyTypes"] and MLFLOW_EGRESS in control["egress"]
    selector = control["podSelector"]["matchLabels"]
    for component in ("gateway", "operator"):
        labels = find(docs, "Deployment", f"kionga-{component}")["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items(), f"the control policy must select the {component}"
    # No other policy restricts the gateway's egress, so the one rule is enough.
    gateway = find(docs, "Deployment", "kionga-gateway")["spec"]["template"]["metadata"]["labels"]
    for policy in (d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"].get("namespace") in (None, "kionga-system")):
        spec = policy["spec"]
        if policy["metadata"]["name"] != "kionga-control" and spec["podSelector"].get("matchLabels", {}).items() <= gateway.items() \
                and "matchExpressions" not in spec["podSelector"]:
            assert "Egress" not in spec["policyTypes"], policy["metadata"]["name"]


def test_chart_denies_mlflow_until_egress_is_listed(tmp_path):
    control = find(render_chart(tmp_path, networkPolicy={"enabled": True, "externalEgress": []}), "NetworkPolicy", "kionga-control")["spec"]
    for rule in control["egress"]:
        assert {"port": 5000} not in rule.get("ports", [])
        assert not [peer for peer in rule.get("to", []) if "ipBlock" in peer]


# Plain manifests (kind) ----------------------------------------------------------

def test_manifest_gateway_uses_the_operators_mlflow_url():
    gateway = env(load("config/deploy/platform-services.yaml"), "kionga-gateway")
    operator = env(load("config/deploy/operator.yaml"), "kionga-operator")
    assert gateway["MLFLOW_URL"]["value"] == operator["MLFLOW_URL"]["value"]
    assert not set(OBJECT_STORE_NAMES) & set(gateway)


def test_manifest_gateway_may_reach_mlflow_in_its_namespace_only():
    address = urlparse(env(load("config/deploy/platform-services.yaml"), "kionga-gateway")["MLFLOW_URL"]["value"])
    assert address.hostname == "mlflow.kionga-system"
    policy = find(load("config/network/platform-allow.yaml"), "NetworkPolicy", "allow-gateway-required-egress")
    assert policy["metadata"]["namespace"] == "kionga-system"
    rules = [rule for rule in policy["spec"]["egress"] if {"protocol": "TCP", "port": address.port} in rule.get("ports", [])]
    assert rules == [{"to": [{"podSelector": {}}], "ports": [{"protocol": "TCP", "port": address.port}]}]
