# Contributing to deploy

Thank you for helping improve Kionga. Read [ARCHITECTURE.md](ARCHITECTURE.md) for
how the deployment targets are organized before you make a larger change.

## Development setup

Prerequisites:

- Docker with Compose v2 and BuildKit
- GNU Make and Bash
- Python 3.11 (the version CI uses) with `pytest`, `pyyaml` and `boto3`
- Helm v3.21.0 and kind v0.31.0 for the Kubernetes paths; `make k8s-tools`
  installs both into `.tools.nosync/bin` (kind needs Go)
- k6, only for `make test-load`

```bash
mkdir kionga && cd kionga
git clone git@github.com:kiongahq/deploy.git
make -C deploy workspace
cd deploy
python -m pip install pytest pyyaml boto3
```

`make workspace` clones the component repositories into `KIONGA_SRC` (the parent
directory by default); `KIONGA_GIT_BASE` changes the clone URL prefix.

## Run the checks

The **CI** workflow clones every component's `main` branch beside this repository
(`bash scripts/workspace.sh`), then runs:

```bash
python -m pytest -q tests            # or: make test
helm lint helm/kionga --strict
bash -n public-up.sh scripts/*.sh
python -m py_compile vm/up.py scripts/verify-production.py scripts/cloud-preflight.py \
  scripts/build_inputs.py objectstore-init.py scripts/migrate_objects.py
```

The tests check Compose wiring, cross-repository contracts, the Helm chart and the
VM installer against real component checkouts, so run `make workspace` first.

The **Pressure** workflow runs nightly, on demand and on pushes to `main`. It builds
every image, boots the core stack, and runs the smoke test, a concurrency stress
test and a k6 load test. To run the same against your local stack:

```bash
make local-up
make smoke
REQUESTS=300 CONCURRENCY=48 make test-stress
make test-load
```

## Change the stack

- A new or changed component setting goes in `compose.yaml` and, when it applies to
  Kubernetes, in `helm/kionga/values.yaml` and the templates. Add a test in
  `tests/`.
- Local ports bind to `KIONGA_BIND_ADDRESS` (default `127.0.0.1`); do not publish a
  port on all interfaces by default (`tests/test_network_exposure.py`).
- Keep secrets out of Git: `.env`, `config/secrets/` and generated files are
  ignored. Document new settings in `.env.example`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/), as the history
does: `type(scope): summary` in the imperative mood, for example
`feat(deploy): private image registries` or
`fix(security): bind local-mode ports to loopback by default`. Common types are
`feat`, `fix`, `test`, `docs`, `ci` and `chore`.

## Pull requests

- Keep one logical change per pull request, with tests for new behavior.
- CI must pass. Do not skip hooks or checks.
- A change that needs a component change lands in that component first; link its
  pull request.

## Reviewed scanner findings

This repository's CI does not run the shared security scanners. If you add Go or
Python code that a scanner flags, follow the convention used across Kionga: in Go,
`// #nosec <rule> -- reason` on the flagged line; in Python, the reason in a comment
on the line before it and `# nosec <rule>` at the end of the flagged line. Fake
credentials that gitleaks reports are pinned by fingerprint in `.gitleaksignore`.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENCE).
