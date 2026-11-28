# Cluster prerequisites

Kionga's chart expects these in the cluster before `applications`, `functions`,
`keda`, `kserve` or `secretStore.openbao` are enabled. The chart's preflight refuses to install and names
the first one that is missing.

| Component | Version | Why |
| --- | --- | --- |
| cert-manager | `v1.21.3` | Wildcard TLS for `*.<applications.baseDomain>` through a DNS-01 `ClusterIssuer`; KServe's webhook certificates |
| KEDA | `2.21.0` | Scaling objects for applications, functions and agents |
| KEDA HTTP add-on | `0.16.0` | Request-based scaling and scale to zero; holds requests while a workload wakes |
| KServe | `v0.21.0` | Model endpoints in Standard (raw) mode: plain Deployments, no Knative, no Istio |
| OpenBao | `2.7.3` (chart `0.30.8`) | The default secret store: three servers on integrated Raft storage, TLS from cert-manager. MPL-2.0 |
| External Secrets Operator | `2.15.0` | Creates the platform Secrets from OpenBao (or a cloud store). Apache-2.0 |
| Reloader | chart `2.2.20` | Rolls Kionga Deployments in the release namespace when a Secret they mount changes. Apache-2.0 |
| Traefik (optional) | chart `42.2.0` | Only with `--ingress traefik`, for kind or a cluster with no controller |

Versions live in [`versions.env`](versions.env); each was at least two weeks old
when it was pinned.

## Install

```bash
helm/prerequisites/install.sh --kube-context my-cluster
helm/prerequisites/install.sh --kube-context my-cluster --dry-run   # print the commands only
helm/prerequisites/install.sh --skip cert-manager                    # you already run it
helm/prerequisites/install.sh --skip openbao                         # you run your own OpenBao
```

Every step is `helm upgrade --install` or `kubectl apply`, so running it again
upgrades in place. `--openbao-values FILE` adds OpenBao chart values after
[`openbao-values.yaml`](openbao-values.yaml) (storage class, resources, a seal
stanza). `--kionga-namespace` names the release namespace Reloader watches
(default `kionga-system`); it gets a Role there and no ClusterRole.

## OpenBao

The install leaves three OpenBao servers running and sealed. Bootstrap them once
from a workstation with `kubectl` access:

```bash
scripts/openbao-bootstrap.sh all --kube-context my-cluster --key-file ~/kionga-openbao-init.json \
  --ca-out openbao-ca.crt
```

- `init` runs only on an uninitialized cluster. It splits the root key into five
  Shamir shares (threshold three, `--key-shares`, `--key-threshold`) and writes
  them with the root token to `--key-file`, mode 600. It refuses to overwrite an
  existing file. Hand the shares to their holders and keep the file off the
  cluster.
- `unseal` opens every sealed server. Run it after any OpenBao pod restarts:
  `scripts/openbao-bootstrap.sh unseal --key-file PATH`. A sealed OpenBao makes
  the SecretStore not Ready, and the chart's preflight names that.
- `configure` mounts KV v2 at `kionga/`, creates the derived transit key
  `kionga-credential` (aes256-gcm96), enables Kubernetes auth and writes one
  policy and role per consumer: `kionga-platform` (External Secrets in the release
  namespace, read on `kionga/<component>`), `kionga-workspace` (read on
  `kionga/workloads/*` and `kionga/model-storage`) and `kionga-gateway` (transit
  encrypt and decrypt with its key and renewing its own token; tokens live one
  hour, at most 24). `--with-database` adds dynamic PostgreSQL credentials for the
  gateway.
- `seed` writes platform secrets without printing them:
  `--from-dir DIR` (files at `DIR/gateway/DATABASE_URL` and so on),
  `--generate gateway/KIONGA_CREDENTIAL_KEY=base64:32` (only when absent) and
  `--rotate PATH/KEY=hex:32` (always new).
- `ca --ca-out FILE` writes the CA certificate for
  `--set-file secretStore.openbao.caBundle=FILE`.
- `reload-tls` makes the servers reread a renewed certificate without sealing.

Every step reads state first, so a rerun changes nothing. The token comes from
`BAO_TOKEN`, or else the root token in the key file; revoke the root token once
you have an admin policy of your own. For an OpenBao you already run, pass
`--addr https://bao.example.com:8200` with a local `bao` CLI: `configure` and
`seed` work, `init` and `unseal` stay yours.

Seals built into OpenBao 2.7.3: Shamir (the default), `transit` (another
OpenBao), `kmip` and `static`. PKCS#11 and the cloud KMS seals are KMS plugins
from openbao/openbao-plugins.

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
4. Bootstrap OpenBao and seed the platform secrets (above).
5. Install the chart with `applications.enabled`, `functions.enabled`,
   `keda.enabled` and `kserve.enabled` set, and `secretStore.openbao.enabled`
   with `externalSecrets.enabled` (see `helm/production.example.yaml`).
