// k6 load test for the native Agent Designer: playground runs end to end
// (gateway → designer runner → gateway callbacks → run record) and the
// designer's read endpoints.
//
//   k6 run tests/load/agent-designer.js                  # full run (~4 min)
//   k6 run -e PROFILE=smoke tests/load/agent-designer.js # ~60 s
//
// MLAIOPS_URL, MLAIOPS_TOKEN or MLAIOPS_LOCAL_USERNAME/PASSWORD as in
// gateway.js. The runner should use the mock model (MLAIOPS_LLM_BACKEND=mock)
// so the numbers measure Kionga, not a model provider.
//
// Scenarios:
//   runs   — constant arrival of playground runs on a customer-support graph
//            (memory → condition → prompt → model), each polled to its end
//   reads  — registry, templates, overview, run history and validation
// A run the runner sheds at capacity (KIONGA_DESIGNER_MAX_RUNS) counts as
// shed, not failed; the shed rate has its own threshold.

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate, Counter } from "k6/metrics";

const baseURL = __ENV.MLAIOPS_URL || "http://localhost:8080";
const token = __ENV.MLAIOPS_TOKEN;
const SHORT = (__ENV.PROFILE || "full") === "smoke";

const runLatency = new Trend("run_end_to_end", true);
const runStart = new Trend("run_start", true);
const readLatency = new Trend("designer_read_latency", true);
const runSucceeded = new Rate("run_succeeded");
const runShed = new Rate("run_shed");
const runsStarted = new Counter("runs_started");

export const options = {
  scenarios: {
    runs: {
      executor: "constant-arrival-rate",
      rate: SHORT ? 4 : 8,
      timeUnit: "1s",
      duration: SHORT ? "45s" : "3m",
      preAllocatedVUs: 20,
      maxVUs: 120,
      exec: "playgroundRun",
    },
    reads: {
      executor: "constant-arrival-rate",
      rate: SHORT ? 30 : 60,
      timeUnit: "1s",
      duration: SHORT ? "45s" : "3m",
      preAllocatedVUs: 10,
      maxVUs: 60,
      exec: "designerReads",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.02"],
    run_succeeded: ["rate>0.98"],
    run_shed: ["rate<0.02"],
    run_start: ["p(95)<500"],
    run_end_to_end: ["p(95)<3000", "p(99)<6000"],
    designer_read_latency: ["p(95)<400", "p(99)<1000"],
  },
};

function signIn() {
  if (token) return { Authorization: `Bearer ${token}` };
  const login = http.post(`${baseURL}/auth/local/login`, {
    username: __ENV.MLAIOPS_LOCAL_USERNAME || "admin",
    password: __ENV.MLAIOPS_LOCAL_PASSWORD || "mlaiops-local",
    return_to: "/console.html",
  }, { redirects: 0 });
  const session = login.cookies.kionga_local_session;
  if (!session || !session[0] || !session[0].value) {
    throw new Error(`sign-in failed (status ${login.status}); set MLAIOPS_TOKEN or local credentials`);
  }
  return { Cookie: `kionga_local_session=${session[0].value}` };
}

// One design from the customer-support template, shared by every virtual user.
export function setup() {
  const headers = { ...signIn(), "Content-Type": "application/json", Accept: "application/json", Origin: baseURL };
  const projects = http.get(`${baseURL}/api/v1/projects`, { headers }).json();
  const project = (Array.isArray(projects) ? projects : projects.items)[0].id;
  const template = http.get(`${baseURL}/api/v1/agent-designs/templates`, { headers }).json()
    .items.find((item) => item.id === "customer-support");
  const design = http.post(`${baseURL}/api/v1/agent-designs`, JSON.stringify({
    project_id: project, name: `load-${Date.now()}`, revision: 0,
    nodes: template.graph.nodes, edges: template.graph.edges, settings: template.graph.settings,
  }), { headers });
  if (design.status !== 201) throw new Error(`create design: ${design.status} ${design.body}`);
  return { headers, design: design.json().id };
}

const MESSAGES = ["I want a refund for order 42", "What are your opening hours?", "Refund status please", "Hello there"];

export function playgroundRun(data) {
  const started = Date.now();
  const response = http.post(`${baseURL}/api/v1/agent-designs/${data.design}/runs`, JSON.stringify({
    input: MESSAGES[Math.floor(Math.random() * MESSAGES.length)], history: [],
  }), { headers: data.headers, tags: { endpoint: "start_run" } });
  runStart.add(response.timings.duration);
  if (!check(response, { "run accepted": (r) => r.status === 202 })) {
    runSucceeded.add(false);
    return;
  }
  runsStarted.add(1);
  const id = response.json().id;
  for (let attempt = 0; attempt < 120; attempt++) {
    sleep(0.1);
    const run = http.get(`${baseURL}/api/v1/agent-designs/${data.design}/runs/${id}`, { headers: data.headers, tags: { endpoint: "poll_run" } });
    if (run.status !== 200) continue;
    const body = run.json();
    if (["succeeded", "failed", "cancelled"].includes(body.status)) {
      const shed = body.status === "failed" && /capacity/.test(body.error || "");
      runShed.add(shed);
      if (!shed) {
        runSucceeded.add(body.status === "succeeded");
        runLatency.add(Date.now() - started);
      }
      return;
    }
  }
  runSucceeded.add(false);
}

const READS = [
  (data) => ["components", `/api/v1/agent-designs/components`],
  (data) => ["templates", `/api/v1/agent-designs/templates`],
  (data) => ["overview", `/api/v1/agent-designs/overview`],
  (data) => ["runs", `/api/v1/agent-designs/${data.design}/runs`],
  (data) => ["design", `/api/v1/agent-designs/${data.design}`],
];

export function designerReads(data) {
  if (Math.random() < 0.2) {
    const response = http.post(`${baseURL}/api/v1/agent-designs/${data.design}/validate`, "{}", { headers: data.headers, tags: { endpoint: "validate" } });
    readLatency.add(response.timings.duration, { endpoint: "validate" });
    check(response, { "validate 200": (r) => r.status === 200 && r.json().runnable === true });
    return;
  }
  const [name, path] = READS[Math.floor(Math.random() * READS.length)](data);
  const response = http.get(`${baseURL}${path}`, { headers: data.headers, tags: { endpoint: name } });
  readLatency.add(response.timings.duration, { endpoint: name });
  check(response, { [`${name} 200`]: (r) => r.status === 200 });
}
