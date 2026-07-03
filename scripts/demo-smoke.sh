#!/usr/bin/env bash
# Full-stack demo smoke: proves every platform capability against a running
# Compose stack. Deterministic: same stack state, same verdicts.
#
#   make local-up && ./scripts/demo-smoke.sh
#
# Exercises: health, project creation, real pipeline execution (Prefect),
# model registry + live serving + prediction, agent deploy + invoke + session
# accounting, feature materialization + online lookup, object storage
# browsing, real-time fraud scoring, prompt/functions surfaces.
set -uo pipefail

GATEWAY="${GATEWAY:-http://localhost:8080}"
FEATURES="${FEATURES:-http://localhost:8083}"
PASS=0; FAIL=0; SKIP=0

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ✓ $*"; }
bad()  { FAIL=$((FAIL+1)); say "  ✗ $*"; }
skip() { SKIP=$((SKIP+1)); say "  - $* (skipped)"; }

jqget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1" 2>/dev/null; }

say "== 1. control plane"
health=$(curl -sf "$GATEWAY/api/v1/health" | jqget "d['status']")
[[ "$health" == "ok" ]] && ok "gateway healthy" || bad "gateway health ($health)"

say "== 2. project"
project_id=$(curl -sf -X POST "$GATEWAY/api/v1/projects" \
  -d "{\"name\":\"smoke-$(date +%s)\",\"template\":\"tabular-classification\"}" | jqget "d['id']")
[[ -n "$project_id" ]] && ok "project created ($project_id)" || bad "project creation"

say "== 3. real pipeline execution"
run_id=$(curl -sf -X POST "$GATEWAY/api/v1/pipelines/submit" \
  -d "{\"project_id\":\"$project_id\",\"name\":\"training-pipeline\"}" | jqget "d['id']")
if [[ -z "$run_id" ]]; then
  bad "pipeline submission"
else
  ok "run submitted ($run_id)"
  status="queued"
  for _ in $(seq 1 60); do
    status=$(curl -sf "$GATEWAY/api/v1/pipelines/runs/$run_id" | jqget "d['status']")
    [[ "$status" == "succeeded" || "$status" == "failed" ]] && break
    sleep 3
  done
  engine=$(curl -sf "$GATEWAY/api/v1/pipelines/runs/$run_id" | jqget "d.get('engine_run_id','')")
  [[ -n "$engine" ]] && ok "engine run linked ($engine)" || bad "no engine run id (Prefect not wired?)"
  [[ "$status" == "succeeded" ]] && ok "pipeline executed to success" || bad "pipeline status: $status"
fi

say "== 4. model registry + live serving"
model_id=$(curl -sf "$GATEWAY/api/v1/models" | jqget "next((i['id'] for i in d['items'] if i['name']=='churn-classifier'),'')")
if [[ -z "$model_id" ]]; then
  bad "churn-classifier not registered by the pipeline"
else
  ok "model registered ($model_id)"
  deploy=$(curl -s -X POST "$GATEWAY/api/v1/models/$model_id/deploy" -d '{"canary_weight":0}')
  endpoint=$(echo "$deploy" | jqget "d.get('endpoint_url','')")
  if [[ "$endpoint" == http* ]]; then
    ok "live endpoint ($endpoint)"
    sleep 20  # mlflow serve cold start
    prediction=$(curl -s -X POST "$GATEWAY/api/v1/models/$model_id/predict" \
      -d '{"inputs": [[0.1,-1.2,0.5,2.0,0.3,-0.7,1.1,0.0,-0.4,0.9,-1.5,0.2]]}')
    echo "$prediction" | grep -q "predictions" && ok "live prediction: $prediction" || bad "prediction failed: $prediction"
  else
    bad "deploy did not produce a live endpoint: $deploy"
  fi
fi

say "== 5. agent lifecycle"
agent_id=$(curl -sf -X POST "$GATEWAY/api/v1/agents" \
  -d "{\"project_id\":\"$project_id\",\"name\":\"customer-support\",\"version\":\"1.0\",\"image\":\"mlaiops/agent-runtime\",\"graph_module\":\"agents.customer_support.graph:build\",\"llm_backend\":\"${MLAIOPS_LLM_BACKEND:-mock}\",\"tools\":[\"feature_store_lookup\",\"kb_search\"]}" | jqget "d['id']")
if [[ -z "$agent_id" ]]; then
  bad "agent deployment"
else
  ok "agent deployed ($agent_id)"
  session_id="smoke-$(date +%s)"
  reply=$(curl -s -X POST "$GATEWAY/api/v1/agents/$agent_id/invoke" \
    -d "{\"message\":\"When are invoices issued?\",\"user_id\":\"u123\",\"session_id\":\"$session_id\"}")
  echo "$reply" | grep -q '"reply"' && ok "agent replied: $(echo "$reply" | jqget "d['reply'][:60]")" || bad "agent invoke: $reply"
  sleep 2
  turns=$(curl -sf "$GATEWAY/api/v1/agents/$agent_id/sessions" | jqget "next((i['turns'] for i in d['items'] if i['id']=='$session_id'),0)")
  [[ "${turns:-0}" -ge 1 ]] && ok "session recorded (turns=$turns)" || bad "session not recorded"
fi

say "== 6. feature store"
feature_count=$(curl -sf "$GATEWAY/api/v1/features" | jqget "d['total']")
[[ "${feature_count:-0}" -ge 2 ]] && ok "feature views applied ($feature_count)" || bad "feature views missing (materializer ran?)"
lookup=$(curl -s -X POST "$FEATURES/get-online-features" \
  -d '{"feature_service":"customer_profile","entities":[{"entity_id":"u123"}]}')
echo "$lookup" | grep -q '"plan"' && ok "online lookup from Redis: $(echo "$lookup" | jqget "d['results'][0]['values'].get('plan')")" || bad "online lookup: $lookup"

say "== 7. object storage"
buckets=$(curl -sf "$GATEWAY/api/v1/storage/buckets" | jqget "len(d['buckets'])")
[[ "${buckets:-0}" -ge 4 ]] && ok "buckets browsable ($buckets)" || bad "storage browse"
snapshot=$(curl -sf "$GATEWAY/api/v1/storage/objects?bucket=mlaiops-features&prefix=customer_profile/" | jqget "len(d['objects'])")
[[ "${snapshot:-0}" -ge 1 ]] && ok "offline snapshot present" || bad "offline snapshot missing"

say "== 8. real-time processing"
if docker compose -f deploy/compose.yaml ps realtime-processor 2>/dev/null | grep -q Up; then
  before=$(curl -sf "$GATEWAY/api/v1/realtime" | jqget "d['demos'].get('fraud',{}).get('events',0)")
  events="$before"
  # Produce from inside the processor container (it has the SDK + httpx, so no
  # host Python deps are required) and re-produce across the polling window:
  # on a cold stack the consumer may still be joining the Kafka group when the
  # first batch lands.
  produce() {
    docker compose -f deploy/compose.yaml exec -T realtime-processor \
      python -m realtime.produce --demo fraud --count 3 >/dev/null 2>&1 \
    || (cd "$(dirname "$0")/.." && KAFKA_REST_URL=http://localhost:8082 \
        PYTHONPATH=python python3 -m realtime.produce --demo fraud --count 3 >/dev/null 2>&1)
  }
  for _ in $(seq 1 8); do
    produce
    for _ in $(seq 1 5); do
      events=$(curl -sf "$GATEWAY/api/v1/realtime" | jqget "d['demos'].get('fraud',{}).get('events',0)")
      [[ "${events:-0}" -gt "${before:-0}" ]] && break 2
      sleep 3
    done
  done
  [[ "${events:-0}" -gt "${before:-0}" ]] && ok "fraud events scored (events=$events)" || bad "realtime pipeline silent"
else
  skip "realtime-processor not running"
fi

say "== 9. observability surfaces"
prompts=$(curl -sf "$GATEWAY/api/v1/prompts" | jqget "d['configured']")
[[ "$prompts" == "True" ]] && ok "Langfuse prompt proxy configured" || skip "Langfuse not configured"
functions=$(curl -sf "$GATEWAY/api/v1/functions" | jqget "d['configured']")
[[ "$functions" == "True" ]] && ok "serverless configured" || skip "OpenFaaS not configured (VM-level)"
curl -sf "http://localhost:3000/api/public/health" >/dev/null && ok "Langfuse UI healthy" || skip "Langfuse UI"

say ""
say "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
