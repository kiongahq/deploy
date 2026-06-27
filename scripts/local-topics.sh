#!/usr/bin/env bash
set -euo pipefail

BROKER="${KAFKA_BROKER:-localhost:9092}"
topics=(
  mlaiops.audit.operations
  mlaiops.pipeline.commands
  mlaiops.model.commands
  mlaiops.agent.commands
  mlaiops.tool.commands
  mlaiops.connection.commands
  mlaiops.llm.traces
  mlaiops.feature.updates
)

for topic in "${topics[@]}"; do
  docker compose -f deploy/compose.yaml exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BROKER" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1
done
