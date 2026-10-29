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
make local-up                 # start; pulls and builds only what is missing
make local-build-up           # also rebuild images whose sources changed
make local-start              # cached images only: no downloads or builds
```

`make local-up` heals the usual local snags on its own:

- **Engine:** if the Podman machine is up but not answering (for example after
  its port forwarder was killed), it restarts the machine; containers and volumes
  are kept. On macOS it starts Docker Desktop when it is closed.
- **Ports:** a busy port from a default or `.env` moves to a free one, and the
  choice is remembered in `.local-state/ports.env` so URLs stay stable. Only a
  port set in the shell for that command (`POSTGRES_PORT=… make local-up`) is
  left as given.
- **Disk:** builds count the host's free space plus space the Podman machine
  already holds but does not use. If that is short, it removes dangling
  (untagged, unused) images; tagged images, containers and volumes are never
  touched. Set `KIONGA_AUTO_PRUNE=0` or `KIONGA_AUTO_RECOVER_ENGINE=0` to opt out.

Open <http://localhost:8080> and sign in with `admin` / `mlaiops-local`. Other
targets: `make local-status`, `make local-logs`, `make local-down`, `make ide-up`
(browser IDE), `make workspace-pull` (update clean checkouts) and `make smoke`
(end-to-end check against the running stack).

Langflow is an optional agent canvas. Start it with `make langflow-up` and stop
it with `make langflow-down`, using the same Docker or Podman engine as the
rest of Kionga. Open **Agents → Langflow** in the console. The `langflow-data`
volume keeps local flows across restarts.

### Operating systems and engine choice

Docker is the default: use Docker Engine with Compose on Linux, or Docker Desktop
on macOS and Windows. On Windows, run the Bash and Make commands inside a WSL 2
Linux distribution with Docker integration enabled; keep checkouts in its Linux
filesystem.

Podman is optional. Use `KIONGA_ENGINE=podman make local-up` after configuring
its rootful engine and a `docker-compose` provider. macOS needs a Podman machine;
Linux and the documented WSL setup use the native Linux engine. The wrapper
selects the Podman socket override and native builds for additional source
contexts. Docker users do not need Podman or this override.

See [OS and engine setup](https://kiongahq.github.io/docs/operations/local/#choose-an-operating-system-and-container-engine)
for the commands, Docker-compatible CLI usage, and validation limits. Keep local
engine preferences and `.env` settings out of version control. Changing engines
does not transfer images or volumes automatically.

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
