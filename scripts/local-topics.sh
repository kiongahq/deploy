#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BROKER="${KAFKA_BROKER:-localhost:9092}"
topics=(
  mlaiops.audit.operations
  mlaiops.pipeline.commands
  mlaiops.model.commands
  mlaiops.agent.commands
  mlaiops.tool.commands
  mlaiops.connection.commands
  mlaiops.workspace.commands
  mlaiops.llm.traces
  mlaiops.feature.updates
  mlaiops.transactions
  mlaiops.fraud.alerts
  mlaiops.callcenter.transcripts
  mlaiops.callcenter.insights
  mlaiops.user.activity
  mlaiops.recs.results
)

existing="$(docker compose -f "$ROOT/deploy/compose.yaml" exec -T kafka \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BROKER" --list)"

for topic in "${topics[@]}"; do
  if printf '%s\n' "$existing" | grep -Fqx "$topic"; then
    continue
  fi
  output="$(docker compose -f "$ROOT/deploy/compose.yaml" exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BROKER" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1 2>&1)" || {
      printf '%s\n' "$output" >&2
      exit 1
    }
  printf '%s\n' "$output" | sed '/^WARNING: Due to limitations in metric names/d'
  existing="${existing}"$'\n'"${topic}"
done
