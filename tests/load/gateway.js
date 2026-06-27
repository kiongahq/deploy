import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate",
      rate: 100,
      timeUnit: "1s",
      duration: "2m",
      preAllocatedVUs: 20,
      maxVUs: 100,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<250", "p(99)<500"],
  },
};

const baseURL = __ENV.MLAIOPS_URL || "http://localhost:8080";

export default function () {
  const response = http.get(`${baseURL}/api/v1/dashboard`, {
    headers: __ENV.MLAIOPS_TOKEN
      ? { Authorization: `Bearer ${__ENV.MLAIOPS_TOKEN}` }
      : {},
  });
  check(response, {
    "dashboard returned 200": (result) => result.status === 200,
    "dashboard is JSON": (result) =>
      (result.headers["Content-Type"] || "").includes("application/json"),
  });
  sleep(0.1);
}
