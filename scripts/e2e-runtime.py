#!/usr/bin/env python3
"""End-to-end checks for Kionga workloads on a live Kubernetes cluster.

Runs against an install made by scripts/kind-ci-up.sh (or any install with the
same features on). Each step is independent and reports pass or fail with its
duration; the run fails if any step failed. Steps:

  application   a KiongaApplication from a public image answers HTTP 200 at
                https://<name>.<baseDomain>
  recovery      a deleted application pod is replaced and the host answers again
  scale-up      load through the ingress raises the application's replica count
  function      a KiongaFunction scales to zero when idle and wakes on a request
  agent         a KiongaAgent becomes Ready with ready endpoints
  model         a KiongaModelDeployment of a registered MLflow model becomes
                Ready and returns a prediction
  pipeline      a container pipeline runs through the gateway's Kubernetes Jobs
                executor to success

OpenBao secret steps (SECRET_STEPS, run with --steps and --openbao-key-file):

  secrets          every ExternalSecret is Ready and every platform Secret is
                   owned by one, so none was created with kubectl
  secret-rotation  a new OIDC_CLIENT_SECRET written to OpenBao reaches the
                   gateway's Secret within the refresh interval and rolls the
                   gateway pods
  workload-secret  an application loads a workload Secret that External Secrets
                   created from OpenBao (labelled kionga.dev/workload-secret)

The objects are created through the Kubernetes API (the operator contract),
except the pipeline, which goes through the gateway API. Pure helpers (status
parsing, polling, load accounting) are unit tested in tests/test_e2e_runtime.py.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import os
import http.cookiejar
import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

# Runs as a numeric non-root user (101) and serves HTTP on 8080: the operator
# runs workloads with runAsNonRoot, which refuses images whose user is root.
WEB_IMAGE = "nginxinc/nginx-unprivileged:1.28.0-alpine@sha256:c97ff0bf7cbae369953c6da1232ec14ad9f971d66360c5698db0856a4cd657a0"
BUSYBOX = "busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec"
IRIS_SAMPLE = [[5.1, 3.5, 1.4, 0.2], [6.7, 3.0, 5.2, 2.3]]
PIPELINE_SUCCESS = {"succeeded", "success", "completed"}
PIPELINE_FAILURE = {"failed", "error", "cancelled", "canceled"}
PLATFORM_SECRETS = {"kionga-system": ["kionga-gateway", "kionga-features", "kionga-storage", "kionga-tracing"],
                    "kionga-workloads": ["kionga-model-storage"]}
WORKLOAD_SECRET_LABEL = "kionga.dev/workload-secret"
BOOTSTRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openbao-bootstrap.sh")


# Pure helpers -------------------------------------------------------------------

class StepFailed(AssertionError):
    pass


def condition(obj: Optional[dict], kind: str = "Ready") -> Optional[str]:
    """The status ("True", "False", "Unknown") of a condition, or None."""
    for item in ((obj or {}).get("status") or {}).get("conditions") or []:
        if item.get("type") == kind:
            return item.get("status")
    return None


def is_ready(obj: Optional[dict]) -> bool:
    return condition(obj, "Ready") == "True"


def describe(obj: Optional[dict]) -> str:
    """One line for a failure message: phase and every condition's reason."""
    if not obj:
        return "not found"
    status = obj.get("status") or {}
    parts = [f"phase={status.get('phase', '?')}"]
    for item in status.get("conditions") or []:
        parts.append(f"{item.get('type')}={item.get('status')}({item.get('reason', '')}: {item.get('message', '')[:160]})")
    return " ".join(parts)


def replicas(deployment: Optional[dict]) -> tuple[int, int]:
    """(desired, ready) for a Deployment; a missing field counts as 0."""
    if not deployment:
        return 0, 0
    return int((deployment.get("spec") or {}).get("replicas") or 0), int((deployment.get("status") or {}).get("readyReplicas") or 0)


def owned_by(items: list[dict], kind: str, name: str) -> Optional[dict]:
    """The first object the named custom resource owns, or one with its name."""
    for item in items:
        for ref in item.get("metadata", {}).get("ownerReferences") or []:
            if ref.get("kind") == kind and ref.get("name") == name:
                return item
    return next((item for item in items if item.get("metadata", {}).get("name") == name), None)


def http_hosts(items: list[dict], kind: str, name: str) -> list[str]:
    """Hosts the KEDA HTTP add-on routes for a workload (HTTPScaledObject.spec.hosts
    or InterceptorRoute rules)."""
    scaled = owned_by(items, kind, name)
    if not scaled:
        return []
    spec = scaled.get("spec") or {}
    hosts = list(spec.get("hosts") or [])
    for rule in spec.get("rules") or []:
        hosts += rule.get("hosts") or ([rule["host"]] if rule.get("host") else [])
    return hosts


def url_host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


def service_from_url(url: str) -> tuple[str, str, int]:
    """(service, namespace, port) from an in-cluster URL such as
    http://iris-predictor.kionga-workloads.svc.cluster.local/v1/models/iris."""
    parts = urllib.parse.urlsplit(url)
    labels = (parts.hostname or "").split(".")
    if len(labels) < 3 or labels[2] != "svc":
        raise ValueError(f"not an in-cluster service URL: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return labels[0], labels[1], port


def predict_path(url: str, inference_service: str) -> str:
    """The KServe v1 predict path for a model endpoint URL. KServe names the
    model after its InferenceService unless the URL already carries a model path."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path.endswith(":predict"):
        return path
    if "/v1/models/" in path:
        return path + ":predict"
    return f"{path}/v1/models/{inference_service}:predict"


def owner_kinds(obj: Optional[dict]) -> set[str]:
    return {ref.get("kind", "") for ref in ((obj or {}).get("metadata") or {}).get("ownerReferences") or []}


def secret_digest(secret: Optional[dict], key: str) -> str:
    """SHA-256 of one Secret value, so steps compare values without holding or
    printing them. Empty when the key is missing."""
    data = ((secret or {}).get("data") or {}).get(key)
    return hashlib.sha256(base64.b64decode(data)).hexdigest() if data else ""


def rolled_out(deployment: Optional[dict], before_template: dict) -> bool:
    """True once the pod template changed and every replica runs the new one."""
    if not deployment:
        return False
    spec, status = deployment.get("spec") or {}, deployment.get("status") or {}
    template = ((spec.get("template") or {}).get("metadata") or {}).get("annotations") or {}
    want = int(spec.get("replicas") or 0)
    return (template != before_template and status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 0)
            and int(status.get("updatedReplicas") or 0) == want and int(status.get("readyReplicas") or 0) == want
            and int(status.get("replicas") or 0) == want)


def pipeline_state(run: dict) -> str:
    """success, failure or running for a gateway pipeline run."""
    status = str(run.get("status") or run.get("state") or "").lower()
    if status in PIPELINE_SUCCESS:
        return "success"
    if status in PIPELINE_FAILURE:
        return "failure"
    return "running"


def poll(check: Callable[[], Any], timeout: float, interval: float = 3.0, what: str = "condition",
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
         describe_last: Callable[[Any], str] = repr) -> Any:
    """Call check until it returns a truthy value; return it. Exceptions count as
    "not yet". On timeout, raise StepFailed with the last observation."""
    deadline = clock() + timeout
    last: Any = None
    while True:
        try:
            last = check()
            if last:
                return last
        except Exception as error:  # noqa: BLE001 - a probe error is an observation
            last = error
        if clock() >= deadline:
            raise StepFailed(f"timed out after {timeout:.0f}s waiting for {what}; last: {describe_last(last)}")
        sleep(interval)


@dataclass
class LoadResult:
    sent: int = 0
    ok: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: str) -> None:
        self.sent += 1
        if outcome == "200":
            self.ok += 1
        else:
            self.errors[outcome] = self.errors.get(outcome, 0) + 1

    @property
    def success_rate(self) -> float:
        return self.ok / self.sent if self.sent else 0.0


def drive_load(request: Callable[[], str], concurrency: int, duration: float,
               clock: Callable[[], float] = time.monotonic) -> LoadResult:
    """Send requests from `concurrency` workers until `duration` passes."""
    result = LoadResult()
    deadline = clock() + duration

    def worker() -> list[str]:
        outcomes = []
        while clock() < deadline:
            try:
                outcomes.append(request())
            except Exception as error:  # noqa: BLE001
                outcomes.append(type(error).__name__)
        return outcomes

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for outcomes in pool.map(lambda _: worker(), range(concurrency)):
            for outcome in outcomes:
                result.record(outcome)
    return result


# Cluster and HTTP access ---------------------------------------------------------

class Kube:
    def __init__(self, context: str, namespace: str):
        self.context, self.namespace = context, namespace

    def run(self, *args: str, input_text: Optional[str] = None, namespace: Optional[str] = None) -> str:
        argv = ["kubectl", "--context", self.context, "-n", namespace or self.namespace, *args]
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[3:])}: {result.stderr.strip()[:500]}")
        return result.stdout

    def get(self, kind: str, name: str, namespace: Optional[str] = None) -> Optional[dict]:
        try:
            return json.loads(self.run("get", kind, name, "-o", "json", namespace=namespace))
        except RuntimeError as error:
            if "NotFound" in str(error) or "not found" in str(error):
                return None
            raise

    def items(self, kind: str, namespace: Optional[str] = None) -> list[dict]:
        return json.loads(self.run("get", kind, "-o", "json", namespace=namespace)).get("items", [])

    def apply(self, manifest: dict) -> None:
        self.run("apply", "-f", "-", input_text=json.dumps(manifest))

    @contextmanager
    def port_forward(self, target: str, port: int, namespace: Optional[str] = None) -> Iterator[int]:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            local = probe.getsockname()[1]
        process = subprocess.Popen(["kubectl", "--context", self.context, "-n", namespace or self.namespace, "port-forward",
                                    target, f"{local}:{port}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            poll(lambda: socket.create_connection(("127.0.0.1", local), timeout=1).close() or True, 30, 0.5,
                 what=f"port-forward to {target}")
            yield local
        finally:
            process.terminate()
            process.wait(timeout=10)


class Http:
    def __init__(self, ca_file: Optional[str]):
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.context),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, url: str, body: Any = None, headers: Optional[dict] = None, form: Optional[dict] = None,
                timeout: float = 30) -> tuple[int, str]:
        data, all_headers = None, dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.status, response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(errors="replace")


# Steps ----------------------------------------------------------------------------

@dataclass
class Settings:
    context: str
    namespace: str
    base_domain: str
    gateway_url: str
    ca_file: Optional[str]
    agent_image: str
    local_user: str
    local_password: str
    model_name: str
    model_version: str
    keda_namespace: str = "keda"
    control_namespace: str = "kionga-system"
    interceptor_service: str = "keda-add-ons-http-interceptor-proxy"
    interceptor_port: int = 8080
    app: str = "e2e-web"
    function: str = "e2e-fn"
    timeout: float = 300
    scale_down_seconds: float = 30
    openbao_key_file: str = ""
    refresh_seconds: float = 10


def application_manifest(s: Settings) -> dict:
    return {"apiVersion": "kionga.dev/v1alpha1", "kind": "KiongaApplication", "metadata": {"name": s.app, "namespace": s.namespace},
            "spec": {"image": WEB_IMAGE, "port": 8080,
                     "replicas": {"min": 1, "max": 4}, "scaling": {"metric": "requests", "target": 1}, "healthPath": "/",
                     "visibility": "public", "resources": {"requests": {"cpu": "20m", "memory": "16Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}}}}


def workload_external_secret(s: Settings, name: str, path: str) -> dict:
    return {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "metadata": {"name": name, "namespace": s.namespace},
            "spec": {"refreshInterval": f"{int(s.refresh_seconds)}s", "secretStoreRef": {"name": "kionga-store", "kind": "SecretStore"},
                     "target": {"name": name, "creationPolicy": "Owner", "deletionPolicy": "Retain",
                                "template": {"metadata": {"labels": {WORKLOAD_SECRET_LABEL: "true"}}}},
                     "data": [{"secretKey": "E2E_TOKEN", "remoteRef": {"key": path, "property": "E2E_TOKEN"}}]}}


def secret_app_manifest(s: Settings, name: str, secret: str) -> dict:
    return {"apiVersion": "kionga.dev/v1alpha1", "kind": "KiongaApplication", "metadata": {"name": name, "namespace": s.namespace},
            "spec": {"image": WEB_IMAGE, "port": 8080, "replicas": {"min": 1, "max": 1}, "healthPath": "/", "visibility": "public",
                     "secretRefs": [{"name": secret}],
                     "resources": {"requests": {"cpu": "20m", "memory": "16Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}}}}


def function_manifest(s: Settings) -> dict:
    return {"apiVersion": "kionga.dev/v1alpha1", "kind": "KiongaFunction", "metadata": {"name": s.function, "namespace": s.namespace},
            "spec": {"image": WEB_IMAGE, "port": 8080,
                     "replicas": {"min": 0, "max": 2}, "scaling": {"metric": "requests", "target": 10}, "healthPath": "/"}}


class Runner:
    def __init__(self, s: Settings, kube: Kube, http_client: Http):
        self.s, self.kube, self.http = s, kube, http_client

    def app_url(self) -> str:
        return f"https://{self.s.app}.{self.s.base_domain}/"

    def app_deployment(self) -> Optional[dict]:
        return owned_by(self.kube.items("deployments"), "KiongaApplication", self.s.app)

    def step_application(self) -> str:
        self.kube.apply(application_manifest(self.s))
        app = poll(lambda: (lambda o: o if is_ready(o) else None)(self.kube.get("kiongaapplication", self.s.app)),
                   self.s.timeout, what="KiongaApplication Ready", describe_last=lambda _: describe(self.kube.get("kiongaapplication", self.s.app)))
        status_url = (app.get("status") or {}).get("url", "")
        if url_host(status_url) != f"{self.s.app}.{self.s.base_domain}":
            raise StepFailed(f"status.url {status_url!r} is not https://{self.s.app}.{self.s.base_domain}")
        code, body = poll(lambda: (lambda r: r if r[0] == 200 else None)(self.http.request("GET", self.app_url())), 180,
                          what=f"HTTP 200 from {self.app_url()}")
        if "nginx" not in body:
            raise StepFailed("the host answered 200 but not from the application")
        return f"{self.app_url()} answered {code}"

    def step_recovery(self) -> str:
        deployment = self.app_deployment()
        desired, _ = replicas(deployment)
        selector = ",".join(f"{k}={v}" for k, v in deployment["spec"]["selector"]["matchLabels"].items())
        pods = json.loads(self.kube.run("get", "pods", "-l", selector, "-o", "json"))["items"]
        if not pods:
            raise StepFailed("the application has no pods to delete")
        victim = pods[0]["metadata"]["name"]
        started = time.monotonic()
        self.kube.run("delete", "pod", victim, "--wait=false")

        def recovered() -> bool:
            names = {p["metadata"]["name"] for p in json.loads(self.kube.run("get", "pods", "-l", selector, "-o", "json"))["items"]}
            return victim not in names and replicas(self.app_deployment())[1] >= max(desired, 1)
        poll(recovered, 120, what="a replacement pod Ready")
        poll(lambda: self.http.request("GET", self.app_url())[0] == 200, 60, what="HTTP 200 after recovery")
        return f"pod {victim} replaced and serving in {time.monotonic() - started:.0f}s"

    def step_scale_up(self) -> str:
        before, _ = replicas(self.app_deployment())
        url = self.app_url()
        peak = {"replicas": before}

        def request() -> str:
            return str(self.http.request("GET", url, timeout=30)[0])

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            # nginx answers in microseconds, so in-flight requests at the
            # interceptor stay low; target 1 per replica makes 60 clients enough.
            load = pool.submit(drive_load, request, 60, 150)

            def scaled() -> bool:
                peak["replicas"] = max(peak["replicas"], replicas(self.app_deployment())[0])
                return peak["replicas"] > before
            try:
                poll(scaled, 160, 5, what=f"more than {before} replicas under load")
            finally:
                result = load.result()
        if result.success_rate < 0.95:
            raise StepFailed(f"only {result.ok}/{result.sent} requests succeeded under load: {result.errors}")
        return f"replicas {before} -> {peak['replicas']}; {result.ok}/{result.sent} requests OK"

    def function_deployment(self) -> Optional[dict]:
        return owned_by(self.kube.items("deployments"), "KiongaFunction", self.s.function)

    def step_function(self) -> str:
        self.kube.apply(function_manifest(self.s))
        poll(lambda: self.kube.get("kiongafunction", self.s.function), 60, what="KiongaFunction")
        poll(self.function_deployment, self.s.timeout, what="the function's Deployment",
             describe_last=lambda _: describe(self.kube.get("kiongafunction", self.s.function)))
        idle = self.s.scale_down_seconds + 240
        poll(lambda: replicas(self.function_deployment()) == (0, 0), idle, 5, what="scale to zero",
             describe_last=lambda _: str(replicas(self.function_deployment())))
        hosts = poll(lambda: http_hosts(self.kube.items("httpscaledobjects.http.keda.sh"), "KiongaFunction", self.s.function)
                     or http_hosts(self.kube.items("interceptorroutes.http.keda.sh"), "KiongaFunction", self.s.function),
                     60, what="the function's HTTP scaling object")
        started = time.monotonic()
        with self.kube.port_forward(f"svc/{self.s.interceptor_service}", self.s.interceptor_port, self.s.keda_namespace) as port:
            plain = Http(None)
            code, body = poll(lambda: (lambda r: r if r[0] == 200 else None)(
                plain.request("GET", f"http://127.0.0.1:{port}/", headers={"Host": hosts[0]}, timeout=120)), 180,
                what="the function to answer after waking")
        if replicas(self.function_deployment())[0] < 1:
            raise StepFailed("the function answered but its Deployment still has 0 replicas")
        return f"scaled to zero, woke on a request to {hosts[0]} in {time.monotonic() - started:.0f}s"

    def step_agent(self) -> str:
        name = "e2e-agent"
        self.kube.apply({"apiVersion": "kionga.dev/v1alpha1", "kind": "KiongaAgent", "metadata": {"name": name, "namespace": self.s.namespace},
                         "spec": {"version": "1", "image": self.s.agent_image, "graphModule": "agents.customer_support.graph:build",
                                  "replicas": {"min": 1, "max": 2}, "scaling": {"metric": "requests", "target": 10}, "llm": {"backend": "mock"}}})
        agent = poll(lambda: (lambda o: o if is_ready(o) else None)(self.kube.get("kiongaagent", name)), self.s.timeout,
                     what="KiongaAgent Ready", describe_last=lambda _: describe(self.kube.get("kiongaagent", name)))
        workload = (agent.get("status") or {}).get("workloadRef") or name
        endpoints = poll(lambda: self._ready_addresses(workload), 120, what=f"ready endpoints for service {workload}")
        return f"agent Ready with {endpoints} ready endpoint(s)"

    def _ready_addresses(self, service: str, namespace: Optional[str] = None) -> int:
        slices = json.loads(self.kube.run("get", "endpointslices", "-l", f"kubernetes.io/service-name={service}", "-o", "json",
                                          namespace=namespace))["items"]
        return sum(1 for s in slices for e in s.get("endpoints") or [] if (e.get("conditions") or {}).get("ready"))

    def step_model(self) -> str:
        name = "e2e-iris"
        self.kube.apply({"apiVersion": "kionga.dev/v1alpha1", "kind": "KiongaModelDeployment", "metadata": {"name": name, "namespace": self.s.namespace},
                         "spec": {"modelName": self.s.model_name, "version": self.s.model_version, "modelFormat": "sklearn",
                                  "replicas": {"min": 1, "max": 1}}})
        deployment = poll(lambda: (lambda o: o if is_ready(o) else None)(self.kube.get("kiongamodeldeployment", name)), self.s.timeout * 2, 5,
                          what="KiongaModelDeployment Ready", describe_last=lambda _: describe(self.kube.get("kiongamodeldeployment", name)))
        status = deployment.get("status") or {}
        if not str(status.get("storageURI", "")).startswith("s3://"):
            raise StepFailed(f"storageURI {status.get('storageURI')!r} was not resolved to the object store")
        service, namespace, port = service_from_url(status["url"])
        predict = predict_path(status["url"], status.get("inferenceServiceRef") or name)
        with self.kube.port_forward(f"svc/{service}", port, namespace) as local:
            code, body = Http(None).request("POST", f"http://127.0.0.1:{local}{predict}", body={"instances": IRIS_SAMPLE}, timeout=60)
        if code != 200:
            raise StepFailed(f"prediction returned {code}: {body[:300]}")
        predictions = json.loads(body).get("predictions")
        if not isinstance(predictions, list) or len(predictions) != len(IRIS_SAMPLE):
            raise StepFailed(f"unexpected prediction body: {body[:300]}")
        return f"{status['url']} predicted {predictions}"

    def step_pipeline(self) -> str:
        # Local sign-in sessions live in one gateway replica's memory, so every
        # call goes to the same pod through a port-forward.
        pods = json.loads(self.kube.run("get", "pods", "-l", "app.kubernetes.io/component=gateway", "--field-selector=status.phase=Running",
                                        "-o", "json", namespace=self.s.control_namespace))["items"]
        if not pods:
            raise StepFailed("no running gateway pod")
        with self.kube.port_forward(f"pod/{pods[0]['metadata']['name']}", 8080, self.s.control_namespace) as port:
            return self._pipeline(f"http://127.0.0.1:{port}", Http(None))

    def _pipeline(self, base: str, client: "Http") -> str:
        origin = {"Origin": self.s.gateway_url.rstrip("/")}
        code, body = client.request("POST", f"{base}/auth/local/login", form={"username": self.s.local_user, "password": self.s.local_password},
                                    headers=origin)
        if code >= 400 or not any(c for c in client.jar):
            raise StepFailed(f"local sign-in failed ({code}): {body[:200]}")

        def api(method: str, path: str, payload: Any = None) -> dict:
            code, text = client.request(method, f"{base}{path}", body=payload, headers=origin)
            if code >= 300:
                raise StepFailed(f"{method} {path} returned {code}: {text[:300]}")
            return json.loads(text) if text else {}

        project = api("POST", "/api/v1/projects", {"name": f"e2e-{int(time.time())}", "description": "kind e2e", "template": "blank"})
        project_id = project.get("id") or project.get("project", {}).get("id")
        definition = api("POST", "/api/v1/pipelines/definitions", {
            "project_id": project_id, "name": "e2e-jobs", "version": "1", "execution_mode": "prefect",
            "jobs": [{"name": "hello", "kind": "container", "image": BUSYBOX, "command": ["sh", "-c", "echo kionga-e2e-ok"],
                      "resources": {"cpu": "100m", "memory": "64Mi"}, "retries": 0, "timeout_seconds": 300}]})
        run = api("POST", "/api/v1/pipelines/submit", {"project_id": project_id, "name": "e2e-jobs", "definition_id": definition.get("id")})
        run_id = run.get("id") or run.get("run", {}).get("id")
        final = poll(lambda: (lambda r: r if pipeline_state(r) != "running" else None)(api("GET", f"/api/v1/pipelines/runs/{run_id}")),
                     self.s.timeout, 5, what=f"pipeline run {run_id} to finish")
        if pipeline_state(final) != "success":
            raise StepFailed(f"pipeline run {run_id} ended {final.get('status')}: {json.dumps(final)[:400]}")
        return f"run {run_id} {final.get('status')} through the Jobs executor"


    # OpenBao secret steps -----------------------------------------------------

    def bootstrap(self, *args: str) -> None:
        if not self.s.openbao_key_file:
            raise StepFailed("needs --openbao-key-file")
        result = subprocess.run(["bash", BOOTSTRAP, *args, "--kube-context", self.s.context, "--key-file", self.s.openbao_key_file],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise StepFailed(f"openbao-bootstrap.sh {args[0]} failed: {result.stderr.strip()[-400:]}")

    def step_secrets(self) -> str:
        checked = 0
        for namespace in (self.s.control_namespace, self.s.namespace):
            externals = self.kube.items("externalsecrets.external-secrets.io", namespace=namespace)
            not_ready = [f"{namespace}/{e['metadata']['name']}: {describe(e)}" for e in externals if not is_ready(e)]
            if not_ready:
                raise StepFailed("ExternalSecrets not Ready: " + "; ".join(not_ready))
            checked += len(externals)
        for namespace, names in PLATFORM_SECRETS.items():
            namespace = self.s.control_namespace if namespace == "kionga-system" else self.s.namespace
            for name in names:
                secret = self.kube.get("secret", name, namespace=namespace)
                if "ExternalSecret" not in owner_kinds(secret):
                    raise StepFailed(f"Secret {namespace}/{name} is not owned by an ExternalSecret")
        return f"{checked} ExternalSecrets Ready; {sum(len(n) for n in PLATFORM_SECRETS.values())} platform Secrets come from OpenBao"

    def step_secret_rotation(self) -> str:
        ns = self.s.control_namespace
        before_secret = secret_digest(self.kube.get("secret", "kionga-gateway", namespace=ns), "OIDC_CLIENT_SECRET")
        if not before_secret:
            raise StepFailed("kionga-gateway has no OIDC_CLIENT_SECRET")
        deployment = self.kube.get("deployment", "kionga-gateway", namespace=ns)
        before_template = ((deployment["spec"]["template"].get("metadata") or {}).get("annotations") or {})
        started = time.monotonic()
        self.bootstrap("seed", "--rotate", "gateway/OIDC_CLIENT_SECRET=hex:24")
        # ESO rereads OpenBao every refresh interval; allow two plus scheduling slack.
        poll(lambda: secret_digest(self.kube.get("secret", "kionga-gateway", namespace=ns), "OIDC_CLIENT_SECRET") != before_secret,
             self.s.refresh_seconds * 2 + 30, 2, what="the rotated value in Secret kionga-gateway")
        synced = time.monotonic() - started
        poll(lambda: rolled_out(self.kube.get("deployment", "kionga-gateway", namespace=ns), before_template), self.s.timeout, 5,
             what="the gateway to roll onto the rotated Secret",
             describe_last=lambda _: str(replicas(self.kube.get("deployment", "kionga-gateway", namespace=ns))))
        return f"Secret updated {synced:.0f}s after the write to OpenBao; gateway rolled in {time.monotonic() - started:.0f}s"

    def step_workload_secret(self) -> str:
        name, path = "e2e-openbao", "workloads/e2e-openbao"
        self.bootstrap("seed", "--rotate", f"{path}/E2E_TOKEN=hex:16")
        self.kube.apply(workload_external_secret(self.s, name, path))
        poll(lambda: is_ready(self.kube.get("externalsecret", name)), 120, 3, what=f"ExternalSecret {name} Ready",
             describe_last=lambda _: describe(self.kube.get("externalsecret", name)))
        secret = self.kube.get("secret", name)
        if ((secret.get("metadata") or {}).get("labels") or {}).get(WORKLOAD_SECRET_LABEL) != "true":
            raise StepFailed(f"Secret {name} lacks {WORKLOAD_SECRET_LABEL}=true")
        want = secret_digest(secret, "E2E_TOKEN")
        app = "e2e-secret-app"
        self.kube.apply(secret_app_manifest(self.s, app, name))
        poll(lambda: is_ready(self.kube.get("kiongaapplication", app)), self.s.timeout, what="KiongaApplication with a workload secret Ready",
             describe_last=lambda _: describe(self.kube.get("kiongaapplication", app)))
        deployment = owned_by(self.kube.items("deployments"), "KiongaApplication", app)
        selector = ",".join(f"{k}={v}" for k, v in deployment["spec"]["selector"]["matchLabels"].items())
        pod = json.loads(self.kube.run("get", "pods", "-l", selector, "--field-selector=status.phase=Running", "-o", "json"))["items"][0]
        have = self.kube.run("exec", pod["metadata"]["name"], "--", "sh", "-c", 'printf %s "$E2E_TOKEN" | sha256sum').split()[0]
        if have != want:
            raise StepFailed("the application's E2E_TOKEN does not match the Secret from OpenBao")
        return f"application {app} loaded E2E_TOKEN from Secret {name} (OpenBao {path})"


STEPS = ["application", "recovery", "scale-up", "function", "agent", "model", "pipeline"]
SECRET_STEPS = ["secrets", "secret-rotation", "workload-secret"]
DEPENDS = {"recovery": "application", "scale-up": "application"}


def run_steps(runner: Runner, selected: list[str]) -> list[dict]:
    results: list[dict] = []
    passed: set[str] = set()
    for name in selected:
        started = time.monotonic()
        needs = DEPENDS.get(name)
        if needs and needs in selected and needs not in passed:
            results.append({"step": name, "status": "skipped", "seconds": 0, "detail": f"needs {needs}"})
            print(f"SKIP {name}: needs {needs}", flush=True)
            continue
        try:
            detail = getattr(runner, "step_" + name.replace("-", "_"))()
            status = "passed"
            passed.add(name)
        except Exception as error:  # noqa: BLE001 - every step reports, the run continues
            detail, status = f"{type(error).__name__}: {error}", "failed"
        seconds = round(time.monotonic() - started, 1)
        results.append({"step": name, "status": status, "seconds": seconds, "detail": detail})
        print(f"{status.upper():6} {name} ({seconds}s): {detail}", flush=True)
    return results


def summary_table(results: list[dict]) -> str:
    """A Markdown table of step results for the workflow summary."""
    lines = ["| Step | Result | Seconds | Detail |", "|---|---|---|---|"]
    for item in results:
        detail = str(item.get("detail", ""))[:200].replace("|", "/").replace("\n", " ")
        lines.append(f"| {item['step']} | {item['status']} | {item['seconds']} | {detail} |")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--summarize"]:
        with open(argv[1]) as handle:
            print(summary_table(json.load(handle)))
        return 0
    parser = argparse.ArgumentParser(description="Kionga Kubernetes e2e checks")
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="kionga-workloads")
    parser.add_argument("--base-domain", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--local-user", default="admin")
    parser.add_argument("--local-password-file", required=True)
    parser.add_argument("--model-name", default="e2e-iris")
    parser.add_argument("--model-version", default="1")
    parser.add_argument("--scale-down-seconds", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--steps", default=",".join(STEPS))
    parser.add_argument("--openbao-key-file", default="", help="OpenBao key file, for the secret steps")
    parser.add_argument("--refresh-seconds", type=float, default=10, help="externalSecrets.refreshInterval of the install")
    parser.add_argument("--report", help="write results as JSON here")
    args = parser.parse_args(argv)
    selected = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = set(selected) - set(STEPS) - set(SECRET_STEPS)
    if unknown:
        parser.error(f"unknown steps: {', '.join(sorted(unknown))}")
    with open(args.local_password_file) as handle:
        password = handle.read().strip()
    settings = Settings(context=args.context, namespace=args.namespace, base_domain=args.base_domain, gateway_url=args.gateway_url,
                        ca_file=args.ca_file, agent_image=args.agent_image, local_user=args.local_user, local_password=password,
                        model_name=args.model_name, model_version=args.model_version, timeout=args.timeout,
                        scale_down_seconds=args.scale_down_seconds, openbao_key_file=args.openbao_key_file,
                        refresh_seconds=args.refresh_seconds)
    results = run_steps(Runner(settings, Kube(args.context, args.namespace), Http(args.ca_file)), selected)
    if args.report:
        with open(args.report, "w") as handle:
            json.dump(results, handle, indent=2)
    failed = [r["step"] for r in results if r["status"] != "passed"]
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed" + (f"; not passed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
