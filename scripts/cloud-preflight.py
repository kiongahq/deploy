#!/usr/bin/env python3
"""Read-only provider and Kubernetes prerequisites for a Kionga staging cluster.

These checks validate reported configuration, not actual policy enforcement, IAM
permissions, storage attachment, disaster recovery, or application behavior.
"""

import argparse
import json
import subprocess
import sys
from urllib.parse import urlparse


def command(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=45)
    return json.loads(result.stdout)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_context_endpoint(context, *expected):
    server = subprocess.run(
        ("kubectl", "config", "view", "--minify", "--context", context,
         "-o", "jsonpath={.clusters[0].cluster.server}"),
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    actual_host = urlparse(server).hostname
    expected_hosts = {urlparse(value if "://" in value else "https://" + value).hostname
                      for value in expected if value}
    require(actual_host and actual_host in expected_hosts, "kubectl context points to a different cluster endpoint")


def check_common(context, storage_class, provisioner):
    nodes = command("kubectl", "--context", context, "get", "nodes", "-o", "json")["items"]
    ready = [node for node in nodes if any(c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", []))]
    require(len(ready) >= 3, "at least three Ready nodes are required")
    zones = {node["metadata"].get("labels", {}).get("topology.kubernetes.io/zone") for node in ready}
    require(len(zones - {None}) >= 2, "Ready nodes must span at least two zones")
    storage = command("kubectl", "--context", context, "get", "storageclass", storage_class, "-o", "json")
    require(storage.get("provisioner") == provisioner, f"{storage_class} uses an unexpected provisioner")
    require(storage.get("allowVolumeExpansion") is True, "storage class must allow expansion")
    require(storage.get("volumeBindingMode") == "WaitForFirstConsumer", "storage class must delay binding until scheduled")
    require(storage.get("reclaimPolicy") == "Retain", "storage class must retain volumes")
    print(f"Kubernetes: {len(ready)} Ready nodes, {len(zones - {None})} zones, {storage_class} configured")


def check_eks(args):
    base = ("aws", "eks")
    cluster = command(*base, "describe-cluster", "--name", args.cluster, "--region", args.region, "--output", "json")["cluster"]
    require(cluster.get("status") == "ACTIVE", "EKS cluster is not ACTIVE")
    check_context_endpoint(args.context, cluster["endpoint"])
    issuer = cluster.get("identity", {}).get("oidc", {}).get("issuer")
    require(issuer, "EKS OIDC issuer missing (required for IRSA)")
    account = cluster["arn"].split(":")[4]
    provider_arn = f"arn:aws:iam::{account}:oidc-provider/{issuer.removeprefix('https://')}"
    providers = command("aws", "iam", "list-open-id-connect-providers", "--output", "json")["OpenIDConnectProviderList"]
    require(any(item.get("Arn") == provider_arn for item in providers), "EKS IAM OIDC provider is not associated for IRSA")
    for addon in ("vpc-cni", "aws-ebs-csi-driver"):
        data = command(*base, "describe-addon", "--cluster-name", args.cluster, "--addon-name", addon, "--region", args.region, "--output", "json")["addon"]
        require(data.get("status") == "ACTIVE", f"EKS add-on {addon} is not ACTIVE")
        if addon == "vpc-cni":
            config = json.loads(data.get("configurationValues") or "{}")
            require(str(config.get("enableNetworkPolicy", "")).lower() == "true", "EKS VPC CNI NetworkPolicy is disabled")
    print("EKS: cluster, IAM OIDC provider, VPC CNI NetworkPolicy, and EBS CSI reported ready")


def check_gke(args):
    data = command("gcloud", "container", "clusters", "describe", args.cluster, "--project", args.project, "--location", args.location, "--format=json")
    require(data.get("status") == "RUNNING", "GKE cluster is not RUNNING")
    check_context_endpoint(args.context, data["endpoint"], data.get("privateClusterConfig", {}).get("privateEndpoint"))
    require(data.get("workloadIdentityConfig", {}).get("workloadPool"), "GKE Workload Identity Federation is disabled")
    dataplane = data.get("networkConfig", {}).get("datapathProvider") == "ADVANCED_DATAPATH"
    legacy_policy = data.get("networkPolicy", {}).get("enabled") is True
    require(dataplane or legacy_policy, "GKE needs Dataplane V2 or enabled NetworkPolicy")
    require(data.get("addonsConfig", {}).get("gcePersistentDiskCsiDriverConfig", {}).get("enabled") is True, "GKE PD CSI driver is disabled")
    print("GKE: cluster, Workload Identity, NetworkPolicy, and PD CSI reported ready")


def check_aks(args):
    data = command("az", "aks", "show", "--resource-group", args.resource_group, "--name", args.cluster, "--output", "json")
    require(data.get("provisioningState") == "Succeeded", "AKS cluster is not provisioned")
    check_context_endpoint(args.context, data.get("privateFqdn"), data.get("fqdn"))
    require(data.get("oidcIssuerProfile", {}).get("enabled") is True, "AKS OIDC issuer is disabled")
    require(data.get("securityProfile", {}).get("workloadIdentity", {}).get("enabled") is True, "AKS Workload Identity is disabled")
    profile = data.get("networkProfile", {})
    require(profile.get("networkPolicy") in ("cilium", "calico", "azure"), "AKS NetworkPolicy engine is missing")
    print("AKS: cluster, OIDC, Workload Identity, and NetworkPolicy reported ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("eks", "gke", "aks"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--region", help="EKS region")
    parser.add_argument("--project", help="GKE project")
    parser.add_argument("--location", help="GKE region or zone")
    parser.add_argument("--resource-group", help="AKS resource group")
    args = parser.parse_args()
    required = {"eks": ("region",), "gke": ("project", "location"), "aks": ("resource_group",)}[args.provider]
    for field in required:
        require(getattr(args, field), f"--{field.replace('_', '-')} is required for {args.provider}")
    provider = {"eks": check_eks, "gke": check_gke, "aks": check_aks}[args.provider]
    storage = {"eks": ("kionga-ebs-gp3", "ebs.csi.aws.com"), "gke": ("kionga-pd-balanced", "pd.csi.storage.gke.io"), "aks": ("kionga-azuredisk-premium", "disk.csi.azure.com")}[args.provider]
    provider(args)
    check_common(args.context, *storage)
    print("Preflight passed. Confirm actual policy enforcement, IAM, PVC provisioning, and failover in staging.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        sys.exit(1)
