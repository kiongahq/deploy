"""Helm: applications, Kionga-native functions, model endpoints, CRDs, preflight
and managed operations access on Kubernetes. Every test renders the chart with
`helm template` and asserts on the objects, never on template text."""

import subprocess

import pytest
import yaml

from test_production_deploy import ROOT, helm_binary, render_chart

APPS = {"enabled": True, "baseDomain": "apps.example.com",
        "tls": {"clusterIssuer": "letsencrypt-dns", "secretName": "apps-tls"}}
ON = {"applications": APPS, "functions": {"enabled": True}, "keda": {"enabled": True}, "kserve": {"enabled": True}}


def with_apps(**applications):
    return dict(ON, applications=dict(APPS, **applications))


def find(docs, kind, name, namespace=None):
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name
               and (namespace is None or d["metadata"].get("namespace") == namespace)]
    assert matches, f"{kind}/{name} not rendered"
    return matches[0]


def env(docs, component):
    deployment = find(docs, "Deployment", f"kionga-{component}")
    return {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}


def grants(role):
    return {(group, resource, verb) for rule in role["rules"] for group in rule["apiGroups"]
            for resource in rule["resources"] for verb in rule["verbs"]}


def source_crds():
    crds = {}
    for path in sorted((ROOT / "config/crd").glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                crds[doc["metadata"]["name"]] = doc
    return crds


def render_raw(tmp_path, *args, **overrides):
    values = yaml.safe_load((ROOT / "helm/production.example.yaml").read_text())
    values["images"] = {name: f"registry/{name}@sha256:" + "a" * 64 for name in values["images"]}
    values.update(overrides)
    file = tmp_path / "values.yaml"
    file.write_text(yaml.safe_dump(values))
    return subprocess.run([helm_binary(), "template", "kionga", str(ROOT / "helm/kionga"), "--namespace", "kionga-system",
                           "-f", str(file), *args], capture_output=True, text=True)


# CRDs ------------------------------------------------------------------------

def test_chart_installs_exactly_the_crds_in_config_crd_and_keeps_them(tmp_path):
    rendered = {d["metadata"]["name"]: d for d in render_chart(tmp_path) if d["kind"] == "CustomResourceDefinition"}
    source = source_crds()
    assert rendered.keys() == source.keys()
    for name, crd in rendered.items():
        assert crd["spec"] == source[name]["spec"], f"{name} differs from config/crd"
        assert crd["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_crds_can_be_left_to_kubectl(tmp_path):
    docs = render_chart(tmp_path, crds={"install": False})
    assert not [d for d in docs if d["kind"] == "CustomResourceDefinition"]


# config/crd for these kinds is generated from the operator's Go types
# (kiongahq/platform, controller-gen); these tests pin the contract the chart,
# the gateway and the e2e lane rely on.

def schema_of(plural):
    return source_crds()[f"{plural}.kionga.dev"]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]


@pytest.mark.parametrize("kind,plural", [("KiongaApplication", "kiongaapplications"), ("KiongaFunction", "kiongafunctions"),
                                         ("KiongaModelDeployment", "kiongamodeldeployments"), ("KiongaAgent", "kiongaagents")])
def test_new_crds_have_status_and_printer_columns(kind, plural):
    crd = source_crds()[f"{plural}.kionga.dev"]
    assert crd["metadata"]["annotations"]["controller-gen.kubebuilder.io/version"], "copy the operator's generated CRD"
    assert crd["spec"]["names"]["kind"] == kind and crd["spec"]["scope"] == "Namespaced"
    [version] = crd["spec"]["versions"]
    assert version["subresources"] == {"status": {}}
    columns = {c["name"]: c["jsonPath"] for c in version["additionalPrinterColumns"]}
    assert columns["Status"] == ".status.phase"
    assert "conditions" in version["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]


@pytest.mark.parametrize("plural", ["kiongaapplications", "kiongafunctions"])
def test_application_and_function_schemas_follow_the_design(plural):
    spec = schema_of(plural)["spec"]
    fields = spec["properties"]
    assert {"image", "source", "port", "env", "secretRefs", "resources", "replicas", "scaling", "healthPath", "visibility"} <= fields.keys()
    assert {"git", "ref", "contextDir", "dockerfile"} <= set(fields["source"]["properties"])
    assert fields["scaling"]["properties"]["metric"]["enum"] == ["requests", "cpu"]
    assert fields["replicas"]["properties"]["min"]["minimum"] == 0
    assert fields["visibility"]["enum"] == ["public", "sso", "internal"]
    assert "has(self.image) != has(self.source)" in " ".join(r["rule"] for r in spec["x-kubernetes-validations"])
    assert {"url", "internalURL", "host", "message", "readyReplicas", "lastBuild", "conditions"} <= schema_of(plural)["status"]["properties"].keys()
    assert "restartedAt" in fields, "redeploy writes spec.restartedAt"
    user = fields["runAsUser"]
    assert (user["type"], user["minimum"], user["maximum"]) == ("integer", 1, 2147483647), "a numeric user, never root"
    assert "runAsUser" not in spec.get("required", []), "the image's own user stays the default"


def test_model_deployment_schema_and_agent_scaling():
    model = schema_of("kiongamodeldeployments")
    assert set(model["spec"]["required"]) == {"modelName", "version"}
    assert model["spec"]["properties"]["modelFormat"]["type"] == "string"
    assert model["spec"]["properties"]["canaryVersion"]["type"] == "string"
    weight = model["spec"]["properties"]["canaryWeight"]
    assert (weight["minimum"], weight["maximum"]) == (0, 100)
    assert {"url", "storageURI", "inferenceServiceRef", "canaryURL", "canaryWeight", "message", "conditions"} <= model["status"]["properties"].keys()
    agent = schema_of("kiongaagents")["spec"]["properties"]
    assert agent["scaling"]["properties"]["metric"]["enum"] == ["requests", "cpu"]
    assert "default" not in agent["scaling"]["properties"]["metric"], "the operator picks requests when KEDA is present"


def test_crd_schemas_have_no_stray_keys():
    """A flow-style YAML value with an unquoted comma once became an unknown
    schema key that the API server refused. Every schema node may hold only
    OpenAPI keywords."""
    allowed = {"type", "format", "description", "properties", "items", "required", "enum", "default", "minimum", "maximum",
               "minLength", "maxLength", "minItems", "maxItems", "pattern", "additionalProperties", "anyOf", "allOf", "oneOf",
               "nullable", "x-kubernetes-int-or-string", "x-kubernetes-preserve-unknown-fields", "x-kubernetes-list-type",
               "x-kubernetes-list-map-keys", "x-kubernetes-map-type", "x-kubernetes-validations", "x-kubernetes-embedded-resource",
               "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "uniqueItems", "maxProperties", "minProperties", "not", "title"}

    def walk(node, path):
        assert set(node) <= allowed, f"{path}: {sorted(set(node) - allowed)}"
        for name, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{name}")
        if isinstance(node.get("items"), dict):
            walk(node["items"], path + "[]")
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], path + "{}")
    for name, crd in source_crds().items():
        for version in crd["spec"]["versions"]:
            walk(version["schema"]["openAPIV3Schema"], name)


# Values, environment ---------------------------------------------------------

def test_features_are_off_by_default_and_say_so_explicitly(tmp_path):
    docs = render_chart(tmp_path)
    for component in ("gateway", "operator"):
        values = env(docs, component)
        assert values["KIONGA_APPLICATIONS_ENABLED"] == "false"
        assert values["KIONGA_FUNCTIONS_ENABLED"] == "false"
        assert values["KIONGA_KEDA_ENABLED"] == "false"
        assert values["KIONGA_KSERVE_ENABLED"] == "false"
        assert "KIONGA_APPLICATIONS_BASE_DOMAIN" not in values and "KIONGA_WEBSITE_URL" not in values
    assert "KIONGA_APPLICATIONS_ENABLED" not in env(docs, "feature-gateway")
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert ("Ingress", "kionga-applications") not in names and ("NetworkPolicy", "kionga-applications") not in names
    assert not any(d["kind"] == "Certificate" for d in docs)


def test_gateway_and_operator_receive_the_runtime_settings(tmp_path):
    docs = render_chart(tmp_path, **with_apps(registry={"url": "registry.example.com/apps", "pushSecretName": "push"}),
                        website={"url": "https://kionga.dev"})
    for component in ("gateway", "operator"):
        values = env(docs, component)
        assert values["KIONGA_APPLICATIONS_BASE_DOMAIN"] == "apps.example.com"
        assert values["KIONGA_APPLICATIONS_TLS_SECRET"] == "apps-tls"
        assert values["KIONGA_INGRESS_CLASS"] == "your-ingress-class"
        assert values["KIONGA_SSO_UPSTREAM"] == "kionga-gateway.kionga-system.svc.cluster.local:8080"
        assert values["KIONGA_APPLICATIONS_NAMESPACE"] == "kionga-workloads"
        assert values["KIONGA_BUILD_REGISTRY"] == "registry.example.com/apps"
        assert values["KIONGA_BUILD_PUSH_SECRET"] == "push"
        assert values["KIONGA_BUILD_REGISTRY_INSECURE"] == "false"
        assert "KIONGA_BUILD_NAMESPACE" not in values, "builds run in the workload namespace"
        assert "@sha256:" in values["KIONGA_BUILDKIT_IMAGE"]
        assert values["KIONGA_KEDA_HTTP_INTERCEPTOR"] == "keda-add-ons-http-interceptor-proxy.keda.svc.cluster.local:8080"
        assert values["KIONGA_KEDA_INTERCEPTOR_URL"] == "http://keda-add-ons-http-interceptor-proxy.keda.svc.cluster.local:8080"
        assert values["KIONGA_TARGET_NAMESPACE"] == "kionga-workloads"
        assert values["KIONGA_BLOG_AUTHORING_ENABLED"] == "false"
        assert values["KIONGA_FUNCTIONS_SCALEDOWN_SECONDS"] == "300"
        assert values["KIONGA_KSERVE_DEPLOYMENT_MODE"] == "RawDeployment"
        assert values["KIONGA_MODEL_STORAGE_SECRET"] == "kionga-model-storage"
        assert values["KIONGA_MODEL_S3_ENDPOINT"] == "" and values["KIONGA_MODEL_S3_USE_HTTPS"] == "1"
        assert values["KIONGA_MODEL_S3_REGION"] == "us-east-1" and values["KIONGA_MLFLOW_ARTIFACTS_DESTINATION"] == ""
        assert values["KIONGA_WEBSITE_URL"] == "https://kionga.dev"
    rendered = "\n".join(yaml.safe_dump(d) for d in docs)
    assert "SERVING_MANAGER_URL" not in rendered and "OPENFAAS_URL" not in rendered


@pytest.mark.parametrize("overrides,message", [
    ({"applications": dict(APPS, baseDomain="")}, "applications.baseDomain must be a DNS name"),
    ({"applications": dict(APPS, baseDomain="example.com"), "keda": {"enabled": True}}, "must not contain kionga.example.com"),
    ({"applications": dict(APPS, baseDomain="workspaces.example.com"), "keda": {"enabled": True}}, "must not contain workspaces.example.com"),
    ({"applications": APPS, "keda": {"enabled": False}}, "applications need keda.enabled"),
    ({"functions": {"enabled": True}}, "functions need keda.enabled"),
    ({"applications": dict(APPS, registry={"url": "r.example.com/a"}), "keda": {"enabled": True}}, "pushSecretName is required"),
    ({"applications": dict(APPS, build={"image": "moby/buildkit:rootless"}), "keda": {"enabled": True}}, "build.image must be an immutable"),
    ({"kserve": {"enabled": True, "deploymentMode": "Serverless"}}, "Kionga does not use Knative"),
    ({"kserve": {"enabled": True, "deploymentMode": "Standard"}}, None),
    ({"website": {"url": "http://kionga.dev"}}, "website.url must be an https"),
])
def test_unsafe_or_incomplete_settings_are_refused(tmp_path, overrides, message):
    result = render_raw(tmp_path, **overrides)
    if message is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0 and message in result.stderr, result.stderr


# Wildcard host and TLS ----------------------------------------------------------

def test_wildcard_certificate_and_fallback_ingress(tmp_path):
    docs = render_chart(tmp_path, **with_apps(fallbackIngress={"enabled": True, "annotations": {
        "traefik.ingress.kubernetes.io/router.priority": "1"}}))
    for namespace in ("kionga-system", "kionga-workloads"):
        cert = find(docs, "Certificate", "kionga-applications-wildcard", namespace)["spec"]
        assert cert["dnsNames"] == ["*.apps.example.com"] and cert["secretName"] == "apps-tls"
        assert cert["issuerRef"] == {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": "letsencrypt-dns"}
    ingress = find(docs, "Ingress", "kionga-applications")
    assert ingress["metadata"]["annotations"] == {"traefik.ingress.kubernetes.io/router.priority": "1"}
    assert ingress["spec"]["ingressClassName"] == "your-ingress-class"
    assert ingress["spec"]["rules"][0]["host"] == "*.apps.example.com"
    assert ingress["spec"]["tls"] == [{"hosts": ["*.apps.example.com"], "secretName": "apps-tls"}]
    assert ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"] == {"name": "kionga-gateway", "port": {"number": 8080}}


def test_existing_wildcard_secret_needs_no_cert_manager(tmp_path):
    docs = render_chart(tmp_path, **with_apps(tls={"clusterIssuer": "", "secretName": "own-tls"}, fallbackIngress={"enabled": False}))
    assert not any(d["kind"] == "Certificate" for d in docs)
    assert not any(d["kind"] == "Ingress" and d["metadata"]["name"] == "kionga-applications" for d in docs)
    assert env(docs, "operator")["KIONGA_APPLICATIONS_TLS_SECRET"] == "own-tls"


# RBAC --------------------------------------------------------------------------

def test_operator_rbac_covers_the_new_workloads_and_stays_namespaced(tmp_path):
    docs = render_chart(tmp_path, **ON)
    operator = grants(find(docs, "Role", "kionga-operator", "kionga-workloads"))
    for group, resource in [("kionga.dev", "kiongaapplications"), ("kionga.dev", "kiongaapplications/status"),
                            ("kionga.dev", "kiongafunctions"), ("kionga.dev", "kiongamodeldeployments/status"),
                            ("apps", "deployments"), ("", "services"), ("networking.k8s.io", "ingresses"),
                            ("policy", "poddisruptionbudgets"), ("", "serviceaccounts"), ("keda.sh", "scaledobjects"),
                            ("http.keda.sh", "httpscaledobjects"), ("http.keda.sh", "interceptorroutes"),
                            ("serving.kserve.io", "inferenceservices"), ("batch", "jobs")]:
        assert (group, resource, "create") in operator and (group, resource, "delete") in operator, resource
    assert ("", "secrets", "get") in operator
    assert ("", "pods/exec", "create") not in operator
    # The namespaced cache also lists watched kinds in the control namespace.
    leader = grants(find(docs, "Role", "kionga-leader"))
    # The live lane found the operator crash-looping on a Job watch it could not list.
    for resource in ("kiongaapplications", "kiongafunctions", "kiongamodeldeployments", "ingresses", "scaledobjects", "httpscaledobjects", "jobs"):
        assert any(r == resource and v == "list" for _, r, v in leader), resource
    assert not any(d["kind"] in ("ClusterRole", "ClusterRoleBinding") for d in docs)


def test_gateway_manages_the_new_resources_and_reads_logs_and_events(tmp_path):
    gateway = grants(find(render_chart(tmp_path, **ON), "Role", "kionga-discovery", "kionga-workloads"))
    for resource in ("kiongaapplications", "kiongafunctions", "kiongamodeldeployments"):
        for verb in ("create", "get", "list", "watch", "update", "delete"):
            assert ("kionga.dev", resource, verb) in gateway
    assert ("", "pods/log", "get") in gateway and ("", "events", "watch") in gateway
    assert ("apps", "deployments", "get") in gateway and ("", "secrets", "get") in gateway
    assert ("kionga.dev", "kiongaagents", "get") in gateway and ("kionga.dev", "kiongaagents", "list") in gateway
    assert not any(resource == "deployments" and verb != "get" for _, resource, verb in gateway)
    assert not any(resource == "pods/exec" for _, resource, _ in gateway)


def test_source_builds_relax_pod_security_only_when_enabled_and_are_fenced(tmp_path):
    plain = find(render_chart(tmp_path), "Namespace", "kionga-workloads")["metadata"]["labels"]
    assert plain["pod-security.kubernetes.io/enforce"] == "restricted"
    # The live lane: KServe predictor pods set no seccomp profile and restricted refused them.
    models = find(render_chart(tmp_path, **ON), "Namespace", "kionga-workloads")["metadata"]["labels"]
    assert models["pod-security.kubernetes.io/enforce"] == "baseline" and models["pod-security.kubernetes.io/warn"] == "restricted"
    docs = render_chart(tmp_path, **with_apps(registry={"url": "r.example.com/apps", "pushSecretName": "push"}))
    labels = find(docs, "Namespace", "kionga-workloads")["metadata"]["labels"]
    # Rootless BuildKit needs unconfined seccomp and AppArmor: neither baseline nor restricted allows it.
    assert labels["pod-security.kubernetes.io/enforce"] == "privileged"
    assert labels["pod-security.kubernetes.io/warn"] == labels["pod-security.kubernetes.io/audit"] == "restricted"
    policy = find(docs, "NetworkPolicy", "kionga-builds", "kionga-workloads")["spec"]
    assert policy["podSelector"]["matchLabels"] == {"app.kubernetes.io/managed-by": "kionga-operator", "app.kubernetes.io/component": "build"}
    assert "ingress" not in policy and policy["policyTypes"] == ["Ingress", "Egress"]
    public = policy["egress"][1]["to"][0]["ipBlock"]
    assert public["cidr"] == "0.0.0.0/0" and {"10.0.0.0/8", "169.254.0.0/16"} <= set(public["except"])
    assert not any(d["kind"] == "Namespace" and d["metadata"]["name"] == "kionga-builds" for d in docs)
    assert ("batch", "jobs", "create") in grants(find(docs, "Role", "kionga-operator", "kionga-workloads"))


def test_kubeflow_is_gone(tmp_path):
    docs = render_chart(tmp_path, **ON)
    rendered = "\n".join(yaml.safe_dump(d) for d in docs)
    assert "kiongapipelineruns" not in rendered and "KFP_URL" not in rendered
    assert "KFP_URL" not in (ROOT / "config/deploy/operator.yaml").read_text()
    assert "kiongapipelineruns.kionga.dev" not in source_crds()
    assert {"kiongatools.kionga.dev", "kiongaconnections.kionga.dev", "kiongamodelpromotions.kionga.dev"} <= source_crds().keys()


# NetworkPolicies ------------------------------------------------------------------

def test_application_pods_accept_only_ingress_controller_interceptor_and_gateway(tmp_path):
    docs = render_chart(tmp_path, **with_apps(extraEgress=[{"to": [{"ipBlock": {"cidr": "203.0.113.0/24"}}]}]))
    policy = find(docs, "NetworkPolicy", "kionga-applications", "kionga-workloads")["spec"]
    assert policy["podSelector"] == {"matchExpressions": [{"key": "kionga.dev/component", "operator": "In",
                                                            "values": ["application", "function"]}]}
    [rule] = policy["ingress"]
    peers = rule["from"]
    assert peers[0] == {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-system"}}}
    assert peers[1]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "keda"}
    assert peers[1]["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "interceptor", "app.kubernetes.io/name": "http-add-on"}
    assert peers[2]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"
    assert len(peers) == 3, "nothing else may reach application pods"
    assert policy["egress"][0]["ports"][0] == {"port": 53, "protocol": "UDP"}
    assert policy["egress"][1] == {"to": [{"ipBlock": {"cidr": "203.0.113.0/24"}}]}
    control = find(docs, "NetworkPolicy", "kionga-control")["spec"]
    assert any(r.get("ports") == [{"port": 8080}] and r["to"][0].get("namespaceSelector", {}).get("matchLabels") ==
               {"kubernetes.io/metadata.name": "keda"} for r in control["egress"]), "the gateway must reach the interceptor"


def test_model_servers_are_fenced_and_reach_only_the_object_store(tmp_path):
    storage = [{"to": [{"ipBlock": {"cidr": "10.0.40.0/24"}}], "ports": [{"port": 443}]}]
    policy = find(render_chart(tmp_path, **dict(ON, kserve={"enabled": True, "storageEgress": storage})),
                  "NetworkPolicy", "kionga-model-serving", "kionga-workloads")["spec"]
    assert policy["podSelector"]["matchExpressions"] == [{"key": "serving.kserve.io/inferenceservice", "operator": "Exists"}]
    assert policy["ingress"][0]["from"][1] == {"podSelector": {"matchExpressions": [{"key": "kionga.dev/agent", "operator": "Exists"}]}}
    assert {"podSelector": {}} not in policy["ingress"][0]["from"], "not every pod in the namespace"
    assert policy["egress"][1:] == storage
    agents = find(render_chart(tmp_path, **ON), "NetworkPolicy", "kionga-agents-to-models", "kionga-workloads")["spec"]
    assert agents["podSelector"] == {"matchExpressions": [{"key": "kionga.dev/agent", "operator": "Exists"}]}
    assert agents["policyTypes"] == ["Egress"]
    assert agents["egress"] == [{"to": [{"podSelector": {"matchExpressions": [
        {"key": "serving.kserve.io/inferenceservice", "operator": "Exists"}]}}]}]


def test_network_policies_follow_the_global_switch(tmp_path):
    docs = render_chart(tmp_path, **ON, networkPolicy={"enabled": False, "externalEgress": []})
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert not names & {"kionga-applications", "kionga-model-serving", "kionga-control"}


# Preflight ---------------------------------------------------------------------

def test_helm_template_without_a_cluster_still_renders(tmp_path):
    assert render_raw(tmp_path, **ON).returncode == 0


@pytest.mark.parametrize("present,missing", [
    ([], "keda.sh/v1alpha1/ScaledObject"),
    (["keda.sh/v1alpha1/ScaledObject"], "http.keda.sh/v1alpha1/HTTPScaledObject"),
    (["keda.sh/v1alpha1/ScaledObject", "http.keda.sh/v1alpha1/HTTPScaledObject"], "serving.kserve.io/v1beta1/InferenceService"),
    (["keda.sh/v1alpha1/ScaledObject", "http.keda.sh/v1alpha1/HTTPScaledObject", "serving.kserve.io/v1beta1/InferenceService"],
     "cert-manager.io/v1/Certificate"),
])
def test_preflight_names_the_first_missing_prerequisite(tmp_path, present, missing):
    args = [arg for version in present + ["external-secrets.io/v1/ExternalSecret", "monitoring.coreos.com/v1/ServiceMonitor"]
            for arg in ("--api-versions", version)]
    result = render_raw(tmp_path, *args, **ON, preflight={"enabled": True, "enforceOffline": True})
    assert result.returncode != 0
    assert f"which this cluster does not serve" in result.stderr and missing in result.stderr
    assert "helm/prerequisites/install.sh" in result.stderr


def test_preflight_passes_when_every_prerequisite_is_served(tmp_path):
    versions = ["keda.sh/v1alpha1/ScaledObject", "http.keda.sh/v1alpha1/HTTPScaledObject", "serving.kserve.io/v1beta1/InferenceService",
                "cert-manager.io/v1/Certificate", "external-secrets.io/v1/ExternalSecret", "monitoring.coreos.com/v1/ServiceMonitor"]
    args = [arg for version in versions for arg in ("--api-versions", version)]
    result = render_raw(tmp_path, *args, **ON, preflight={"enabled": True, "enforceOffline": True})
    assert result.returncode == 0, result.stderr


def test_preflight_requires_kionga_crds_when_the_chart_does_not_install_them(tmp_path):
    result = render_raw(tmp_path, "--api-versions", "external-secrets.io/v1/ExternalSecret", "--api-versions",
                        "monitoring.coreos.com/v1/ServiceMonitor", crds={"install": False},
                        preflight={"enabled": True, "enforceOffline": True})
    assert result.returncode != 0 and "kionga.dev/v1alpha1/KiongaAgent" in result.stderr and "config/crd" in result.stderr


def test_preflight_can_be_turned_off(tmp_path):
    assert render_raw(tmp_path, **ON, preflight={"enabled": False, "enforceOffline": True}).returncode == 0


# Managed operations access -------------------------------------------------------

def test_operations_access_is_off_by_default(tmp_path):
    docs = render_chart(tmp_path)
    assert not any(d["metadata"].get("labels", {}).get("kionga.dev/access") == "operations" for d in docs)


def test_operations_access_is_scoped_labelled_and_never_reads_secrets_by_default(tmp_path):
    docs = render_chart(tmp_path, **ON, pipelineExecutor={"kubernetes": True, "namespace": "kionga-pipelines"},
                        operations={"access": {"enabled": True, "subjects": [{"kind": "Group", "name": "kionga-operations"}]}})
    granted = [d for d in docs if d["metadata"].get("labels", {}).get("kionga.dev/access") == "operations"]
    roles = [d for d in granted if d["kind"] == "Role"]
    assert sorted(r["metadata"]["namespace"] for r in roles) == ["kionga-pipelines", "kionga-system", "kionga-workloads"]
    for role in roles:
        rules = grants(role)
        assert ("apps", "deployments", "patch") in rules and ("", "pods", "delete") in rules
        assert ("", "pods/log", "get") in rules and ("", "events", "list") in rules
        assert ("kionga.dev", "kiongaapplications", "get") in rules and ("serving.kserve.io", "inferenceservices", "list") in rules
        assert not any(resource == "*" for _, resource, _ in rules), "enumerate resources; no wildcard"
        assert not any(resource == "secrets" for _, resource, _ in rules)
        assert not any(resource in ("pods/exec", "pods/attach", "pods/portforward") for _, resource, _ in rules)
        assert not any(group == "rbac.authorization.k8s.io" or verb in ("impersonate", "escalate", "bind") for group, _, verb in rules)
        writes = {(g, r, v) for g, r, v in rules if v not in ("get", "list", "watch")}
        assert writes == {("apps", "deployments", "patch"), ("apps", "deployments/scale", "update"),
                          ("apps", "deployments/scale", "patch"), ("", "pods", "delete")}
    cluster = grants(find(granted, "ClusterRole", "kionga-operations"))
    assert {verb for _, _, verb in cluster} <= {"get", "list", "watch"}
    assert not any(resource == "secrets" for _, resource, _ in cluster)
    binding = find(granted, "ClusterRoleBinding", "kionga-operations")
    assert binding["subjects"] == [{"kind": "Group", "name": "kionga-operations", "apiGroup": "rbac.authorization.k8s.io"},
                                   {"kind": "ServiceAccount", "name": "kionga-operations", "namespace": "kionga-system"}]
    account = find(granted, "ServiceAccount", "kionga-operations")
    assert account["automountServiceAccountToken"] is False


def test_operations_secret_read_needs_the_explicit_flag(tmp_path):
    docs = render_chart(tmp_path, operations={"access": {"enabled": True, "subjects": [{"kind": "User", "name": "ops@kionga.dev"}],
                                                         "serviceAccount": {"create": False, "name": "x"}, "allowSecretRead": True}})
    role = find(docs, "Role", "kionga-operations", "kionga-system")
    assert ("", "secrets", "get") in grants(role)
    assert not any(d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "x" for d in docs)


@pytest.mark.parametrize("access,message", [
    ({"enabled": True, "subjects": [], "serviceAccount": {"create": False, "name": "x"}}, "needs subjects or serviceAccount.create"),
    ({"enabled": True, "subjects": [{"kind": "ServiceAccount", "name": "x"}]}, "must be kind User or Group"),
])
def test_operations_access_refuses_bad_subjects(tmp_path, access, message):
    result = render_raw(tmp_path, operations={"access": access})
    assert result.returncode != 0 and message in result.stderr


def test_private_ca_reaches_every_component_without_replacing_system_roots(tmp_path):
    assert "SSL_CERT_DIR" not in env(render_chart(tmp_path), "gateway")
    docs = render_chart(tmp_path, trustedCA={"configMapName": "corp-ca", "key": "ca.crt"})
    for deployment in (d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].startswith("kionga-")
                       and "app.kubernetes.io/instance" in d["spec"]["template"]["metadata"]["labels"]):
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        values = {e["name"]: e.get("value") for e in container["env"]}
        assert values["SSL_CERT_DIR"] == "/etc/ssl/certs:/etc/kionga/ca"
        assert {"name": "trusted-ca", "mountPath": "/etc/kionga/ca", "readOnly": True} in container["volumeMounts"]
        assert {"name": "trusted-ca", "configMap": {"name": "corp-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}} in pod["volumes"]


def test_kind_lane_values_render_with_every_feature_on(tmp_path):
    images = {name: f"localhost:5001/kionga/{name}@sha256:" + "b" * 64 for name in
              yaml.safe_load((ROOT / "helm/kionga/values.yaml").read_text())["images"]}
    file = tmp_path / "images.yaml"
    file.write_text(yaml.safe_dump({"images": images}))
    result = subprocess.run([helm_binary(), "template", "kionga", str(ROOT / "helm/kionga"), "--namespace", "kionga-system",
                             "-f", str(ROOT / "kind/ci/values.yaml"), "-f", str(file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    values = env(docs, "operator")
    assert values["KIONGA_APPLICATIONS_BASE_DOMAIN"] == "apps.127.0.0.1.sslip.io"
    assert values["KIONGA_FUNCTIONS_SCALEDOWN_SECONDS"] == "30"
    gateway = env(docs, "gateway")
    assert gateway["KIONGA_EXECUTOR"] == "kubernetes" and gateway["SSL_CERT_DIR"].endswith("/etc/kionga/ca")
    assert values["KIONGA_MODEL_S3_ENDPOINT"] == "objectstore.kionga-data.svc:9000" and values["KIONGA_MODEL_S3_USE_HTTPS"] == "0"
    assert values["KIONGA_MLFLOW_ARTIFACTS_DESTINATION"] == "s3://kionga-models"
    assert find(docs, "Ingress", "kionga-applications")["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.priority"] == "1"


def _unknown_fields(value, schema, path="spec"):
    """Fields in value that the structural schema does not declare."""
    if isinstance(value, dict) and "properties" in schema:
        found = []
        for key, item in value.items():
            if key not in schema["properties"]:
                found.append(f"{path}.{key}")
            else:
                found += _unknown_fields(item, schema["properties"][key], f"{path}.{key}")
        return found
    return []


def test_crd_examples_cover_every_new_kind_and_use_declared_fields():
    crds = {doc["spec"]["names"]["kind"]: doc for doc in source_crds().values()}
    valid = [d for d in yaml.safe_load_all((ROOT / "tests/crd-examples/valid.yaml").read_text()) if d]
    assert {"KiongaApplication", "KiongaFunction", "KiongaModelDeployment", "KiongaAgent"} <= {d["kind"] for d in valid}
    for doc in valid:
        schema = crds[doc["kind"]]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
        assert not _unknown_fields(doc["spec"], schema), doc["metadata"]["name"]
    invalid = sorted((ROOT / "tests/crd-examples/invalid").glob("*.yaml"))
    assert len(invalid) >= 10
    for path in invalid:
        doc = yaml.safe_load(path.read_text())
        assert doc["kind"] in crds and path.read_text().startswith("#"), path.name


def test_static_operator_rbac_matches_the_chart(tmp_path):
    """scripts/kind-up.sh applies config/rbac/operator.yaml instead of the chart;
    it must grant everything the chart's operator Role does."""
    static = next(d for d in yaml.safe_load_all((ROOT / "config/rbac/operator.yaml").read_text()) if d and d["kind"] == "ClusterRole")
    chart = find(render_chart(tmp_path, **ON), "Role", "kionga-operator", "kionga-workloads")
    missing = grants(chart) - grants(static)
    assert not missing, sorted(missing)
