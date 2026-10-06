#!/usr/bin/env python3
"""Read-only Kubernetes and HTTP acceptance checks for a staged Kionga release."""

import argparse
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


def kubectl(context, namespace, *args):
    result = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, *args, "-o", "json"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def check_cluster(context, namespace, release, workspace_namespace, expect_monitoring):
    deployments = kubectl(context, namespace, "get", "deployments", "-l", f"app.kubernetes.io/instance={release}")
    components = {item["metadata"]["name"].removeprefix(release + "-"): item for item in deployments["items"]}
    required = {"gateway", "operator", "integration-worker", "feature-gateway", "storage-proxy", "trace-proxy"}
    assert required <= components.keys(), f"missing deployments: {required - components.keys()}"
    for name in required:
        item = components[name]
        desired = item["spec"].get("replicas", 1)
        assert desired >= 2 and item.get("status", {}).get("readyReplicas", 0) >= desired, f"{name} replicas are not ready"
        container = item["spec"]["template"]["spec"]["containers"][0]
        assert "@sha256:" in container["image"], f"{name} image is not pinned"
        assert container.get("readinessProbe") and container.get("livenessProbe"), f"{name} probes are missing"
    budgets = {item["metadata"]["name"] for item in kubectl(context, namespace, "get", "pdb")["items"]}
    assert {release + "-" + name for name in required} <= budgets, "a component disruption budget is missing"
    control_policies = {item["metadata"]["name"] for item in kubectl(context, namespace, "get", "networkpolicy")["items"]}
    assert release + "-control" in control_policies and release + "-edge" in control_policies, "control network policies are missing"
    ingress = kubectl(context, namespace, "get", "ingress", release)
    assert ingress["spec"].get("tls"), "control ingress has no TLS"
    workspace_ingress = kubectl(context, namespace, "get", "ingress", release + "-workspaces")
    assert workspace_ingress["spec"].get("tls"), "workspace ingress has no TLS"
    assert workspace_ingress["spec"]["rules"][0]["host"].startswith("*."), "workspace ingress is not isolated by hostname"
    for route in (ingress, workspace_ingress):
        secret_name = route["spec"]["tls"][0]["secretName"]
        certificate = kubectl(context, namespace, "get", "secret", secret_name)
        assert certificate.get("type") == "kubernetes.io/tls" and {"tls.crt", "tls.key"} <= certificate.get("data", {}).keys(), f"TLS Secret {secret_name} is incomplete"
    hpa = kubectl(context, namespace, "get", "hpa", release + "-gateway")
    assert hpa["spec"]["minReplicas"] >= 2, "gateway HPA minimum is below two"
    hpa_conditions = hpa.get("status", {}).get("conditions", [])
    assert any(c["type"] == "AbleToScale" and c["status"] == "True" for c in hpa_conditions), "gateway HPA is not able to scale"
    workspace_ns = kubectl(context, namespace, "get", "namespace", workspace_namespace)
    assert workspace_ns["metadata"]["labels"].get("pod-security.kubernetes.io/enforce") == "restricted"
    policies = {item["metadata"]["name"]: item for item in kubectl(context, workspace_namespace, "get", "networkpolicy")["items"]}
    isolation = policies.get(release + "-workload-isolation")
    assert isolation and set(isolation["spec"]["policyTypes"]) == {"Ingress", "Egress"}, "workload network isolation policy is missing"
    external_secrets = kubectl(context, namespace, "get", "externalsecrets")["items"]
    for item in external_secrets:
        conditions = item.get("status", {}).get("conditions", [])
        assert any(c["type"] == "Ready" and c["status"] == "True" for c in conditions), f"ExternalSecret {item['metadata']['name']} is not ready"
    if expect_monitoring:
        monitors = {item["metadata"]["name"] for item in kubectl(context, namespace, "get", "servicemonitors")["items"]}
        assert {release + "-gateway", release + "-operator", release + "-integration-worker"} <= monitors, "component ServiceMonitors are missing"
        rules = {item["metadata"]["name"] for item in kubectl(context, namespace, "get", "prometheusrules")["items"]}
        assert release + "-availability" in rules, "availability PrometheusRule is missing"
    nodes = kubectl(context, namespace, "get", "nodes")["items"]
    ready_nodes = [node for node in nodes if any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])]
    assert len(ready_nodes) >= 3, "fewer than three ready nodes"
    zones = {node["metadata"].get("labels", {}).get("topology.kubernetes.io/zone") for node in ready_nodes}
    assert len(zones - {None}) >= 2, "ready nodes span fewer than two zones"
    print("Cluster: replicas, probes, digests, TLS ingress, HPA, isolation, and node spread pass")


def request(url, token=""):
    status, body, _ = exchange(url, token=token)
    return status, body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def exchange(url, *, method="GET", token="", cookie="", origin="", data=None, content_type="application/json"):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = content_type
    request_object = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(NoRedirect)
    try:
        with opener.open(request_object, timeout=12) as response:
            return response.status, response.read(1_000_000), response.headers
    except HTTPError as error:
        return error.code, error.read(1_000_000), error.headers


def check_certificate(origin, minimum_days):
    parsed = urlparse(origin)
    assert parsed.scheme == "https" and parsed.hostname, "certificate check requires an HTTPS hostname"
    with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=12) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=parsed.hostname) as tls:
            certificate = tls.getpeercert()
    expiry = ssl.cert_time_to_seconds(certificate["notAfter"])
    assert expiry - time.time() >= minimum_days * 86400, f"TLS certificate for {parsed.hostname} expires within {minimum_days} days"


def check_http(origin, workspace_host, admin_token, user_token, forbidden_project, minimum_cert_days):
    origin = origin.rstrip("/")
    assert origin.startswith("https://"), "public origin must be HTTPS"
    assert workspace_host.startswith("https://") and workspace_host != origin, "workspace host must be a separate HTTPS origin"
    check_certificate(origin, minimum_cert_days)
    check_certificate(workspace_host, minimum_cert_days)
    checks = [
        (origin + "/api/v1/ready", "", {200}, "readiness"),
        (origin + "/api/v1/me", "", {401}, "anonymous API denial"),
        (origin + "/api/v1/me", admin_token, {200}, "admin identity"),
        (origin + "/api/v1/me", user_token, {200}, "user identity"),
        (origin + "/api/v1/admin/users", admin_token, {200}, "admin provisioning access"),
        (origin + "/api/v1/admin/users", user_token, {403}, "user provisioning denial"),
        (origin + "/api/v1/projects/" + forbidden_project, admin_token, {200}, "cross-project fixture existence"),
        (origin + "/api/v1/projects/" + forbidden_project, user_token, {403, 404}, "cross-project denial"),
        (origin + "/workspaces/workbench/lab", user_token, {404}, "same-origin workspace denial"),
        (workspace_host.rstrip("/") + "/workspaces/workbench/lab", "", {401}, "anonymous workspace denial"),
    ]
    for url, token, expected, label in checks:
        status, _ = request(url, token)
        assert status in expected, f"{label}: received {status}, expected {sorted(expected)}"
    print("HTTP: TLS, identity, provisioning, project denial, and workspace origin checks pass")


def check_workspace_handoff(origin, workspace_host, user_token):
    status, body, _ = exchange(origin.rstrip("/") + "/api/v1/workspaces/workbench/launch", method="POST", token=user_token, data=b'{"project_id":""}')
    assert status == 200, f"workspace launch returned {status}"
    handoff = json.loads(body)
    destination = handoff["url"]
    assert destination == workspace_host.rstrip("/") + "/_kionga/redeem", "launch used the wrong workspace origin"
    payload = urlencode({"ticket": handoff["ticket"], "next": handoff["next"]}).encode()
    status, _, headers = exchange(destination, method="POST", origin=origin.rstrip("/"), data=payload, content_type="application/x-www-form-urlencoded")
    assert status == 303 and headers.get("Location", "").startswith("/workspaces/workbench/"), "handoff did not redirect to workspace"
    set_cookie = headers.get("Set-Cookie", "")
    assert "kionga_workspace_session=" in set_cookie and "Secure" in set_cookie and "HttpOnly" in set_cookie and "Domain=" not in set_cookie, "workspace cookie is not host-only and secure"
    cookie = set_cookie.split(";", 1)[0]
    status, _, _ = exchange(destination, method="POST", origin=origin.rstrip("/"), data=payload, content_type="application/x-www-form-urlencoded")
    assert status == 401, "handoff ticket could be replayed"
    status, _, _ = exchange(workspace_host.rstrip("/") + "/workspaces/workbench/api", cookie=cookie)
    assert status == 200, f"authenticated workspace API returned {status}"
    status, _, _ = exchange(workspace_host.rstrip("/") + "/_kionga/logout", method="POST", cookie=cookie, origin=workspace_host.rstrip("/"))
    assert status == 204, f"workspace logout returned {status}"
    status, _, _ = exchange(workspace_host.rstrip("/") + "/workspaces/workbench/api", cookie=cookie)
    assert status == 401, "revoked workspace cookie remained usable"
    print("Workspace: one-time handoff, host-only cookie, API access, and logout pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="kionga-system")
    parser.add_argument("--workspace-namespace", default="kionga-workloads")
    parser.add_argument("--release", default="kionga")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--workspace-host", required=True, help="a provisioned workbench hostname")
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument("--user-token-file", type=Path, required=True)
    parser.add_argument("--forbidden-project", required=True, help="project not assigned to test user")
    parser.add_argument("--test-workspace-handoff", action="store_true", help="create and revoke an ephemeral workspace session")
    parser.add_argument("--expect-monitoring", action="store_true", help="require chart ServiceMonitors and alert rules")
    parser.add_argument("--min-cert-days", type=int, default=14)
    args = parser.parse_args()
    admin_token = args.admin_token_file.read_text().strip()
    user_token = args.user_token_file.read_text().strip()
    assert admin_token and user_token and admin_token != user_token, "distinct test identity tokens are required"
    check_cluster(args.context, args.namespace, args.release, args.workspace_namespace, args.expect_monitoring)
    check_http(args.origin, args.workspace_host, admin_token, user_token, args.forbidden_project, args.min_cert_days)
    if args.test_workspace_handoff:
        check_workspace_handoff(args.origin, args.workspace_host, user_token)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Acceptance failed: {error}", file=sys.stderr)
        sys.exit(1)
