# deploy

[![CI](https://github.com/kiongahq/deploy/actions/workflows/ci.yml/badge.svg)](https://github.com/kiongahq/deploy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENCE)

Run Kionga: on your machine with Docker Compose, on a single server, or on Kubernetes.

## Background

Kionga is a self-hosted platform for machine learning, data and AI agents, built
from several component repositories. This repository runs them together:

- a local stack (`compose.yaml`) that builds every image from sibling checkouts,
- a single-server install (`vm/`) from released images pinned by digest,
- a Helm chart (`helm/kionga`) with production values and EKS, GKE and AKS
  overlays, plus a kind cluster for local Kubernetes testing.

## Install

You need Git, Docker with Compose, and GNU Make.

```bash
mkdir kionga && cd kionga
git clone git@github.com:kiongahq/deploy.git
make -C deploy workspace      # clones the component repositories beside deploy
```

## Usage

```bash
cd deploy
make local-up                 # builds what changed and starts the stack
```

Open <http://localhost:8080> and sign in with `admin` / `mlaiops-local`. Other
targets: `make local-status`, `make local-logs`, `make local-down`, `make ide-up`
(browser IDE), `make workspace-pull` (update clean checkouts) and `make smoke`
(end-to-end check against the running stack).

For a server or a cluster:

```bash
make public-up DEPLOYMENT=/etc/kionga/deployment.json    # single server
make k8s-tools && make kind-up                           # local Kubernetes
```

## Documentation

- Product documentation: [Install Kionga](https://kiongahq.github.io/docs/getting-started/installation/),
  [Run locally](https://kiongahq.github.io/docs/operations/local/),
  [Single server](https://kiongahq.github.io/docs/operations/public-vm/),
  [Deploy on Kubernetes](https://kiongahq.github.io/docs/operations/managed-kubernetes/)
  and [Single sign-on](https://kiongahq.github.io/docs/operations/single-sign-on/)
- Engineering handbook: <https://kiongahq.github.io/docs/engineering/>
- How this repository is laid out: [ARCHITECTURE.md](ARCHITECTURE.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the checks CI
runs and the commit conventions.

## License

[MIT](LICENCE) © Kionga contributors
