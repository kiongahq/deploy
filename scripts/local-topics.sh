#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BROKER="${KAFKA_BROKER:-localhost:9092}"
topics=(
  kionga.audit.operations
  kionga.pipeline.commands
  kionga.model.commands
  kionga.agent.commands
  kionga.tool.commands
  kionga.connection.commands
  kionga.workspace.commands
  kionga.llm.traces
  kionga.feature.updates
  kionga.transactions
  kionga.fraud.alerts
  kionga.callcenter.transcripts
  kionga.callcenter.insights
  kionga.user.activity
  kionga.recs.results
)

existing="$(bash "$ROOT/scripts/container.sh" compose -f "$ROOT/compose.yaml" exec -T kafka \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BROKER" --list)"

for topic in "${topics[@]}"; do
  if printf '%s\n' "$existing" | grep -Fqx "$topic"; then
    continue
  fi
  output="$(bash "$ROOT/scripts/container.sh" compose -f "$ROOT/compose.yaml" exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BROKER" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1 2>&1)" || {
      printf '%s\n' "$output" >&2
      exit 1
    }
  printf '%s\n' "$output" | sed '/^WARNING: Due to limitations in metric names/d'
  existing="${existing}"$'\n'"${topic}"
done
