# deploy

How [Kionga](https://github.com/kiongahq)'s components run together: the local
stack, a single production VM, and Kubernetes.

| Path | What it is |
| --- | --- |
| `compose.yaml` | Local stack, built from sibling checkouts of the component repositories |
| `compose.public.yaml`, `caddy/`, `dex/` | TLS edge and OIDC login for a public VM |
| `vm/` | Production single-VM install from released, digest-pinned images |
| `helm/` | Kubernetes chart, production values example and EKS/GKE/AKS overlays |
| `config/` | Cluster manifests: CRDs, RBAC, network policies, backups |
| `kind/` | Local Kubernetes cluster for the fidelity path |
| `mlflow/`, `postgres/`, `objectstore-init.py` | Images and bootstrap for the bundled data services |
| `scripts/` | Start-up, smoke, stress, kind and cloud preflight tooling |

## Run it locally

```bash
mkdir kionga && cd kionga
git clone git@github.com:kiongahq/deploy.git && cd deploy
make workspace      # clones platform, serving, trace-proxy, agent-runtime,
                    # pipeline-runner, sdk-python, workspace, contracts, docs beside it
make local-up       # builds what changed and starts the stack
```

Then open <http://localhost:8080>. `make local-up` rebuilds only images whose
sources changed, including changes in another repository an image depends on (for
example an SDK edit rebuilds `agent-runtime` and `jupyter`). `make workspace-pull`
fast-forwards clean checkouts.

The checkouts live in `KIONGA_SRC`, the parent of this repo by default:

```text
kionga/
  deploy/  platform/  serving/  trace-proxy/  agent-runtime/
  pipeline-runner/  sdk-python/  workspace/  contracts/  docs/
```

| Image | Built from |
| --- | --- |
| gateway, feature-gateway, storage-proxy, log-exporter | `platform/go` |
| serving-manager | `serving` |
| trace-proxy | `trace-proxy` |
| agent-runtime, realtime-processor, feature-materializer | `agent-runtime` (+ `sdk-python`) |
| pipeline-runner | `pipeline-runner` |
| jupyter, ide | `workspace` (+ `sdk-python`, `agent-runtime`, `pipeline-runner`) |
| mlflow | `mlflow/` here |

By default the Python images install the SDK from your `sdk-python` checkout.
`KIONGA_SDK_SOURCE=release make local-rebuild` builds them against the tagged
releases instead, which is what CI publishes.

| Service | Port |
| --- | --- |
| Console and API (gateway) | `8080` (`GATEWAY_PORT`) |
| Jupyter workbench | through the console, or `8888` |
| MLflow | `15000` |
| Prefect | `4200` |
| Langfuse | `3000` |
| Object store (RustFS) S3 API / console | `9000` / `9001` |
| trace-proxy, kafka-rest, feature-gateway, storage-proxy, serving-manager, log-exporter | `8081`-`8086` |

`make local-status`, `make local-logs`, `make local-down`. Optional profiles:
`make ide-up` (browser IDE), `--profile log-export` (log exporter, see the
[log export guide](https://kiongahq.github.io/docs/operations/log-export/)).

## Object storage

The bundled S3-compatible store is [RustFS](https://github.com/rustfs/rustfs)
(Apache-2.0), pinned by digest in `compose.yaml` as the `objectstore` service. It
also answers to the old `minio` hostname, so `http://minio:9000` settings keep
working. MinIO is no longer bundled: its images were removed from Docker Hub and
quay.io stopped serving anonymous pulls in September 2026.

If you ran Kionga with MinIO before, your buckets are still in the old
`mlaiops_minio-data` volume and `make local-up` reminds you until they are copied:

```bash
make migrate-minio                 # needs the old MinIO image still on this machine
tail -f /tmp/minio-to-rustfs/progress.log
```

It copies every bucket and object, verifies each by size and MD5, resumes if
interrupted, and writes `report.csv` and `summary.json` to `/tmp/minio-to-rustfs/`.
The MinIO volume is only read; delete it yourself when you are satisfied. Only the
current version of each object is copied. To use another S3 store, point the
`*_S3_ENDPOINT*` settings at it and drop the `objectstore` service.

## Single VM (production pilot)

`vm/compose.yaml` runs released images pinned by digest from
`ghcr.io/kiongahq/*`; it never builds from source.

```bash
make public-up DEPLOYMENT=/etc/kionga/deployment.json
```

See [Public VM](https://kiongahq.github.io/docs/operations/public-vm/).

## Kubernetes

```bash
make k8s-tools      # pinned kind and helm in .tools.nosync/bin
make kind-up        # local cluster with images built from the checkouts
make test-e2e-kind
```

For managed clusters use `helm/production.example.yaml` with a provider overlay
from `helm/providers/`, and `scripts/deploy-kubernetes.sh`. See
[Managed Kubernetes](https://kiongahq.github.io/docs/operations/managed-kubernetes/).

## Tests

```bash
make test           # compose wiring, cross-repo contracts, Helm chart, VM installer
make smoke          # against a running stack
```

`make test` needs the component checkouts (`make workspace`) and, for the chart
tests, Helm (`make k8s-tools`). CI clones every component's `main` branch, so a
change that breaks how the pieces fit fails here. The nightly **Pressure** workflow
builds every image, boots the stack and runs smoke, concurrency and k6 load tests.
