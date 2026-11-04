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
- **Workspace tokens:** `JUPYTER_TOKEN`, `IDE_PASSWORD`,
  `KIONGA_WORKSPACE_JOB_TOKEN` and `KIONGA_SANDBOX_TOKEN` are random, generated once and kept in
  `.local-state/secrets.env` (mode 0600, ignored by Git) so restarts keep working.
  A value in the shell or in `.env` wins, and must be at least 32 characters.
  To rotate them, delete the file and run `make local-up` again.
- **Trace proxy keys:** the same file holds a random `TRACE_PROXY_PROJECT_KEY`, which
  agents use to call the trace proxy, and `TRACE_PROXY_KEYS`, the proxy's list holding
  only that key's SHA-256. For real OpenAI-compatible models, put the provider key in
  `LLM_UPSTREAM_API_KEY` in `.env`, not `OPENAI_API_KEY`: the proxy holds it and agents
  never see it. `make public-up` and other deployments never read this file; set both
  values yourself (`.env.example` has the commands).
- **Model artifact reader:** the file also holds `SERVING_OBJECTSTORE_ACCESS_KEY` and
  `SERVING_OBJECTSTORE_SECRET_KEY`. `objectstore-init` creates that user with read-only
  access to `mlaiops-models` and `mlaiops-artifacts`, and the serving manager downloads
  artifacts with it instead of the store's root user.
- **Disk:** builds count the host's free space plus space the Podman machine
  already holds but does not use. If that is short, it removes dangling
  (untagged, unused) images; tagged images, containers and volumes are never
  touched. Set `KIONGA_AUTO_PRUNE=0` or `KIONGA_AUTO_RECOVER_ENGINE=0` to opt out.

Open the console URL printed by startup (normally [http://127.0.0.1:8080](http://127.0.0.1:8080))
and sign in with `admin` / `mlaiops-local`. The numeric address matches the container
binding and readiness check. Single sign-on also requires the configured callback
host (normally `localhost`) to reach this same gateway. Stop a standalone
`make run` gateway on the same port before using the Compose stack. Other
targets: `make local-status`, `make local-logs`, `make local-down`, `make ide-up`
(browser IDE), `make workspace-pull` (update clean checkouts) and `make smoke`
(end-to-end check against the running stack).

### Network exposure

The local stack ships development credentials, so every port it publishes binds to
`KIONGA_BIND_ADDRESS`, which defaults to `127.0.0.1`. Nothing is reachable from
other machines until you widen it:

| Setting | Effect |
| --- | --- |
| unset (default) | Every published port, including Postgres `5432`, Redis `6379` and Kafka `9092`, listens on `127.0.0.1` only |
| `KIONGA_BIND_ADDRESS=0.0.0.0` | The console, APIs and data services listen on every interface; use only on a trusted network after changing the default credentials |
| any value | Jupyter (`8888`), the log exporter and its search backends stay on `127.0.0.1` |

`compose.public.yaml` removes every host port except Caddy's `80` and `443`
(`tests/test_network_exposure.py`, `tests/test_sso.py`).
It also refuses to start with any built-in local credential: the
`public-preflight` service (and `public-up.sh`) runs `scripts/public-preflight.sh`,
which fails when a credential listed in `.env.example` is unset or equals a local
default, and the services that hold those credentials wait for it to succeed.

Langflow is an optional agent canvas. Start it with `make langflow-up` and stop
it with `make langflow-down`, using the same Docker or Podman engine as the
rest of Kionga. Open **Agents → Langflow** in the console. The `langflow-data`
volume keeps local flows across restarts.

### Sandboxes

Agent team sandboxes run under gVisor (`runsc`) through `sandbox-manager`, which
holds engine socket access, publishes no port and is reachable only by the gateway
on an internal network. `make local-up` generates `KIONGA_SANDBOX_TOKEN`. The rest
of the stack does not depend on this service.

| Machine | Result |
| --- | --- |
| Engine runs `runsc`, `KIONGA_SANDBOX_IMAGES` lists an image by digest | Sandboxes work |
| `KIONGA_SANDBOX_IMAGES` empty (the local default) | The manager stays up and creates return 503 `sandbox_unavailable`; set it in `.env` |
| Engine has no `runsc` (default on macOS and Windows) | Sandbox tools report `sandbox_unavailable`; there is no fallback |
| Single server (`vm/`) | Off until `agent_teams.sandbox.enabled` in `deployment.json`; `vm/up.py` starts nothing unless a real container runs under gVisor |
| Legacy public overlay | `runsc` is pinned, `public-up.sh` stops unless a real container runs under gVisor, and the preflight rejects the unsafe flag and requires `KIONGA_SANDBOX_IMAGES` |
| Helm | Needs a gVisor RuntimeClass; the chart has no setting for the unsafe flag |

On a Linux host, `sudo make sandbox-runtime-install` is an optional way to install
`runsc` and register it with Docker. It downloads gVisor's release archive, checks
it against a SHA-512 pinned in `scripts/install-runsc.sh`, and installs `runsc`, the
containerd shim and the `gvisor-bin/` directory `runsc` needs beside it. Nothing else runs it. `make sandbox-runtime-verify` starts a container and
reads its kernel log. An engine's runtime list is not proof: Podman lists `runsc`
even when it is not installed.

For development on a machine without gVisor, you can opt in to a weaker runtime by
adding both lines to `.env`, then running `make local-up`:

```bash
KIONGA_SANDBOX_RUNTIME=runc
KIONGA_SANDBOX_ALLOW_UNSAFE_RUNTIME=1
```

This shares the host kernel with code the model wrote, so a kernel exploit escapes
the sandbox. Do not use it with untrusted input. The manager refuses it when
`KIONGA_ENVIRONMENT=production` and logs a warning on every create.

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
- Engineering handbook: [https://kiongahq.github.io/docs/engineering/](https://kiongahq.github.io/docs/engineering/)
- How this repository is laid out: [ARCHITECTURE.md](ARCHITECTURE.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the checks CI
runs and the commit conventions.

## License

[MIT](LICENCE) © Kionga contributors
