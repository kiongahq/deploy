# Cluster prerequisites

Kionga's chart expects these in the cluster before `applications`, `functions`,
`keda` or `kserve` are enabled. The chart's preflight refuses to install and names
the first one that is missing.

| Component | Version | Why |
| --- | --- | --- |
| cert-manager | `v1.21.3` | Wildcard TLS for `*.<applications.baseDomain>` through a DNS-01 `ClusterIssuer`; KServe's webhook certificates |
| KEDA | `2.21.0` | Scaling objects for applications, functions and agents |
| KEDA HTTP add-on | `0.16.0` | Request-based scaling and scale to zero; holds requests while a workload wakes |
| KServe | `v0.21.0` | Model endpoints in Standard (raw) mode: plain Deployments, no Knative, no Istio |
| Traefik (optional) | chart `42.2.0` | Only with `--ingress traefik`, for kind or a cluster with no controller |

Versions live in [`versions.env`](versions.env); each was at least two weeks old
when it was pinned.

## Install

```bash
helm/prerequisites/install.sh --kube-context my-cluster
helm/prerequisites/install.sh --kube-context my-cluster --dry-run   # print the commands only
helm/prerequisites/install.sh --skip cert-manager                    # you already run it
```

Every step is `helm upgrade --install`, so running it again upgrades in place.

### Why a script and not helmfile

The script is plain `helm upgrade --install` commands with pinned versions. Helm is
already required for the Kionga chart, so it adds no tool, and `--dry-run` prints
exactly what will run for a change review. KServe publishes its charts as release
assets, which the script installs by URL at the pinned tag.

## Ingress controller

Pick your own and set `ingress.className`. Kionga does not default to
ingress-nginx: the project was retired upstream in 2026. Two requirements:

- Ingresses for request-scaled workloads point at the KEDA interceptor through an
  `ExternalName` Service; enable ExternalName backends in the controller (Traefik:
  `providers.kubernetesIngress.allowExternalNameServices=true`, which the script sets).
- Exact hosts must win over the fallback `*.<baseDomain>` Ingress. Traefik orders
  routes by rule length, so give the fallback the lowest priority:
  `applications.fallbackIngress.annotations: {traefik.ingress.kubernetes.io/router.priority: "1"}`.

## After the prerequisites

1. Create a DNS-01 `ClusterIssuer` for your DNS provider and set
   `applications.tls.clusterIssuer`, or create a `kubernetes.io/tls` Secret for
   `*.<baseDomain>` named `applications.tls.secretName` in the release namespace and
   in `workspaceNamespace`.
2. Point a wildcard DNS record `*.<baseDomain>` at the ingress controller.
3. For model endpoints, create `kserve.storageSecretName` in `workspaceNamespace`
   with a read-only object store identity.
4. Install the chart with `applications.enabled`, `functions.enabled`,
   `keda.enabled` and `kserve.enabled` set.
