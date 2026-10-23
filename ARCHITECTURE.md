# Architecture

This document describes how the deploy repository is organized and how it puts the
Kionga components together.

## Bird's-eye view

The component repositories each build one or more images. This repository holds
no application code; it decides how those images run. There are three targets:

| Target | Entry point | Images |
| --- | --- | --- |
| Local stack | `make local-up` (`compose.yaml`) | Built from sibling checkouts in `KIONGA_SRC` |
| Single server | `make public-up` (`vm/up.py`, `vm/compose.yaml`) | Released `ghcr.io/kiongahq/*` images pinned by digest |
| Kubernetes | `helm/kionga`, `scripts/deploy-kubernetes.sh` | Released images; kind builds from checkouts |

The local stack runs PostgreSQL (pgvector), Kafka with a Kafka REST proxy, Redis,
RustFS object storage, MLflow, Prefect, Langfuse, Dex with a GLAuth test directory,
and the Kionga services: `gateway`, `feature-gateway`, `storage-proxy`,
`serving-manager`, `trace-proxy`, `pipeline-runner`, `agent-runtime`,
`feature-materializer`, `realtime-processor` and `jupyter`, plus the optional `ide`
and log exporter profiles.

## Code map

### Compose

- `compose.yaml`: the local stack. Build contexts point at `${KIONGA_SRC:-..}/<repo>`.
- `compose.public.yaml`, `caddy/Caddyfile`: TLS edge (Caddy) and OIDC login for
  a public server built from the local stack (`public-up.sh`).
- `sso/`: Dex configuration (`dex.yaml`), the GLAuth test directory (`glauth.cfg`)
  and the login-page provider template (`providers.tmpl`).
- `mlflow/Dockerfile`: the MLflow server image, which doubles as the model server
  image (`mlaiops-mlflow`). `postgres/init.sql` and `objectstore-init.py` bootstrap
  databases and buckets.
- `.env.example`: every setting operators put in `.env`.

### `vm/`

`up.py` deploys `vm/compose.yaml` (gateway and Caddy) from a non-secret deployment
JSON (`deployment.example.json`), a config file (`config.example.json`) and secret
files materialized outside Git.

### `helm/`

`helm/kionga` is the chart (`kubeVersion >= 1.29`): workloads, config, RBAC,
network policies, edge and monitoring templates. `production.example.yaml`,
`providers/` (EKS, GKE, AKS and storage classes) and `secretstore-*.example.yaml`
are starting points for real clusters.

### `config/`

Plain manifests: CRDs (`crd/`), operator and integration-worker deployments
(`deploy/`), RBAC, default-deny and allow network policies, optional Istio strict
mTLS, PostgreSQL backups, Dex and Vault security settings.

### `kind/`, `scripts/`

- `workspace.sh`: clone or fast-forward every component (`make workspace`).
- `local-up.sh` with `build_inputs.py`: rebuild only images whose inputs changed
  (Dockerfile, build arguments and copied files, across repositories), retry
  pulls, move busy host ports, and start the stack.
- `demo-smoke.sh`, `stress-test.sh`, `tests/load/gateway.js`: smoke, concurrency
  and k6 load tests.
- `bootstrap-k8s-tools.sh`, `kind-up.sh`, `e2e-kind.sh`, `deploy-kubernetes.sh`,
  `cloud-preflight.py`, `verify-production.py`: the Kubernetes path.
- `migrate-minio-to-rustfs.sh` with `migrate_objects.py`: copy an older MinIO
  volume into RustFS, verified per object.

### `tests/`

Gate tests: local bootstrap, port fallback, network exposure, restart policy, SSO
wiring, the MLflow server, object migration and the production deploy path.

## Invariants

- **Loopback by default.** The local stack publishes ports on
  `KIONGA_BIND_ADDRESS` (default `127.0.0.1`), because it uses development
  credentials.
- **Production never builds from source.** `vm/compose.yaml` and the chart use
  released images by digest; `vm/compose.yaml` is never merged with
  `compose.yaml`.
- **Secrets stay out of Git and out of plain config.** They arrive as environment
  variables or `*_FILE` files.
- **One ML version set.** MLflow and scikit-learn in `mlflow/Dockerfile` match the
  pipeline runner and the Jupyter image.
- **The stack resumes whole** after Docker or the host restarts
  (`restart: unless-stopped`).
- **The Docker socket is a trust boundary.** Only `serving-manager` and
  `pipeline-runner` mount it.

## How it connects to other components

CI and `make workspace` clone platform, serving, trace-proxy, agent-runtime,
pipeline-runner, sdk-python, workspace, contracts and docs at `main`, so a change
in any of them that breaks how the pieces fit fails here. The list of repositories
lives in `scripts/workspace.sh`; the organization's full list is `repos.txt` in
[kiongahq/.github](https://github.com/kiongahq/.github).

## Cross-cutting concerns

- **Testing.** Gate tests on every push; the Pressure workflow boots the real stack
  nightly. See [CONTRIBUTING.md](CONTRIBUTING.md).
- **Documentation.** Operator-facing behavior is documented in the product
  documentation under Deploy and operate; keep it in step with this repository.
