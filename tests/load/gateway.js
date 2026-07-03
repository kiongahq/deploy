// k6 load + pressure test for the Nexus control plane.
//
//   k6 run tests/load/gateway.js                  # full run (~5 min)
//   k6 run -e PROFILE=smoke tests/load/gateway.js # short CI-friendly run (~90s)
//
// Three scenarios exercise the gateway the way the console does:
//   reads   — steady constant-rate traffic across every hot read endpoint
//   spike   — a ramping burst that pressures the API at several times the rate
//   writes  — lower-rate write pressure on the transactional control plane
//
// SLOs are asserted as thresholds; a breach fails the run (and CI). Latency
// budgets are intentionally generous because CI co-locates the whole stack on
// one runner — tighten them for a dedicated load box.

import http from "k6/http";
import { check } from "k6";
import { Trend, Rate } from "k6/metrics";

const baseURL = __ENV.MLAIOPS_URL || "http://localhost:8080";
const token = __ENV.MLAIOPS_TOKEN;
const profile = __ENV.PROFILE || "full";

const readLatency = new Trend("read_latency", true);
const writeLatency = new Trend("write_latency", true);
const writeErrors = new Rate("write_errors");

// Every read endpoint the console polls, with the check each must satisfy.
const READ_ENDPOINTS = [
  { path: "/api/v1/health", name: "health" },
  { path: "/api/v1/me", name: "me" },
  { path: "/api/v1/dashboard", name: "dashboard" },
  { path: "/api/v1/projects", name: "projects" },
  { path: "/api/v1/pipelines/runs", name: "runs" },
  { path: "/api/v1/models", name: "models" },
  { path: "/api/v1/agents", name: "agents" },
  { path: "/api/v1/features", name: "features" },
  { path: "/api/v1/catalog", name: "catalog" },
  { path: "/api/v1/components", name: "components" },
  { path: "/api/v1/realtime", name: "realtime" },
  { path: "/api/v1/audit", name: "audit" },
];

const SHORT = profile === "smoke";

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate",
      rate: SHORT ? 80 : 150,
      timeUnit: "1s",
      duration: SHORT ? "60s" : "3m",
      preAllocatedVUs: 30,
      maxVUs: 150,
      exec: "readMix",
    },
    spike: {
      executor: "ramping-arrival-rate",
      startRate: 20,
      timeUnit: "1s",
      preAllocatedVUs: 40,
      maxVUs: 300,
      startTime: SHORT ? "60s" : "3m",
      stages: SHORT
        ? [
            { target: 400, duration: "15s" },
            { target: 400, duration: "10s" },
            { target: 20, duration: "5s" },
          ]
        : [
            { target: 600, duration: "40s" },
            { target: 600, duration: "40s" },
            { target: 20, duration: "20s" },
          ],
      exec: "readMix",
    },
    writes: {
      executor: "constant-arrival-rate",
      rate: SHORT ? 5 : 10,
      timeUnit: "1s",
      duration: SHORT ? "30s" : "2m",
      preAllocatedVUs: 10,
      maxVUs: 40,
      exec: "writeMix",
    },
  },
  thresholds: {
    // Overall availability: fewer than 2% of requests may fail.
    http_req_failed: ["rate<0.02"],
    // Read latency budget (generous for a co-located CI runner).
    read_latency: ["p(95)<400", "p(99)<1000"],
    // Writes go through the transactional outbox; allow more headroom.
    write_latency: ["p(95)<800", "p(99)<1500"],
    write_errors: ["rate<0.02"],
  },
};

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function readMix() {
  const endpoint = READ_ENDPOINTS[Math.floor(Math.random() * READ_ENDPOINTS.length)];
  const response = http.get(`${baseURL}${endpoint.path}`, {
    headers: authHeaders(),
    tags: { endpoint: endpoint.name },
  });
  readLatency.add(response.timings.duration, { endpoint: endpoint.name });
  check(response, {
    [`${endpoint.name} 200`]: (r) => r.status === 200,
    [`${endpoint.name} json`]: (r) =>
      (r.headers["Content-Type"] || "").includes("application/json"),
  });
}

export function writeMix() {
  // Pressure the transactional control plane: each create writes a resource,
  // an audit record, and outbox entries in one transaction.
  const payload = JSON.stringify({
    name: `load-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
    template: "tabular-classification",
    description: "k6 write pressure",
  });
  const response = http.post(`${baseURL}/api/v1/projects`, payload, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    tags: { endpoint: "create_project" },
  });
  writeLatency.add(response.timings.duration);
  const created = check(response, {
    "create_project 201": (r) => r.status === 201,
    "create_project returns id": (r) => {
      try {
        return typeof r.json().id === "string";
      } catch (_) {
        return false;
      }
    },
  });
  writeErrors.add(!created);
}
