# Run Kionga: the local stack from sibling checkouts, a single VM, or Kubernetes.
.PHONY: workspace workspace-pull test local local-up local-start local-build-up local-rebuild local-down local-status local-logs \
        ide-up ide-agents-up ide-down langflow-up langflow-down public-up public-down kind-up k8s-tools test-e2e-kind \
        test-load test-stress smoke

COMPOSE := bash scripts/container.sh compose -f compose.yaml

workspace:                 ## clone every component beside this repo
	bash scripts/workspace.sh

workspace-pull:            ## fast-forward clean component checkouts
	bash scripts/workspace.sh --pull

test:                      ## deploy gate tests (compose wiring, Helm chart, VM installer)
	python -m pytest -q tests

local: local-up

local-up:                  ## start cached images without downloading or building
	bash scripts/local-up.sh

local-start: local-up       ## alias for cached-only startup

local-build-up:            ## build stale images inside Docker/Podman, then start
	KIONGA_BUILD_MODE=auto bash scripts/local-up.sh

local-rebuild:
	KIONGA_BUILD_MODE=auto KIONGA_REBUILD=1 bash scripts/local-up.sh

local-down:
	$(COMPOSE) down

local-status:
	$(COMPOSE) ps --all

local-logs:
	$(COMPOSE) logs --tail 100

ide-up:
	$(COMPOSE) --profile ide up -d ide

ide-agents-up:
	KIONGA_IDE_AGENTS=true $(COMPOSE) --profile ide up -d --build ide

ide-down:
	$(COMPOSE) --profile ide stop ide

langflow-up:                ## start the optional Langflow canvas and scoped API helper
	$(COMPOSE) --profile langflow up -d --build gateway langflow langflow-sidecar

langflow-down:              ## stop Langflow but keep its flow data volume
	$(COMPOSE) --profile langflow stop langflow-sidecar langflow

sso-reload:                ## re-render Dex after changing KIONGA_*_CLIENT_ID/SECRET in .env (the login page follows)
	$(COMPOSE) up -d --force-recreate --wait dex

smoke:                     ## end-to-end smoke test against a running stack
	GATEWAY=$${GATEWAY:-http://localhost:8080} bash scripts/demo-smoke.sh

public-up:                 ## single VM with released images (DEPLOYMENT=/etc/kionga/deployment.json)
	bash public-up.sh "$(DEPLOYMENT)"

public-down:
	python3 vm/up.py "$(DEPLOYMENT)" --stop

k8s-tools:                 ## pinned kind and helm in .tools.nosync/bin
	bash scripts/bootstrap-k8s-tools.sh

kind-up:
	bash scripts/kind-up.sh

test-e2e-kind:
	bash scripts/e2e-kind.sh

test-load:
	k6 run tests/load/gateway.js

test-stress:
	bash scripts/stress-test.sh
