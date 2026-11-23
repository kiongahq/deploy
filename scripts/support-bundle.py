#!/usr/bin/env python3
"""Collect a redacted support bundle from a Kionga install on Kubernetes.

Read-only: it runs `helm` and `kubectl get/logs` and writes one .tar.gz. It
never reads Secrets, every literal environment value is removed, and every
collected text passes through redact() before it is written. Collected: tool and cluster versions, the Helm release (status,
history, user-supplied values), nodes, prerequisite CRDs, Kionga custom
resources with their status, workloads, pods, events, and recent logs of every
container in the Kionga namespaces.

Usage:
  scripts/support-bundle.py [--context CTX] [--namespace kionga-system]
      [--release kionga] [--since 2h] [--tail 2000] [--output FILE]
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REDACTED = "[REDACTED]"

# Order matters: whole structures first, then key=value forms, then bare tokens.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    # scheme://user:password@host keeps the scheme, user and host.
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1" + REDACTED + r"\2"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*\"?)(bearer|basic|token)\s+[^\s\",]+"), r"\1\2 " + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), REDACTED),  # JWT
    (re.compile(r"\bnxs_[A-Za-z0-9_-]{8,}"), REDACTED),  # Kionga API tokens
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),  # AWS access key IDs
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),  # GitHub tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), REDACTED),  # model provider keys
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), REDACTED),  # Slack tokens
]


# JSON or YAML fields and key=value pairs whose name says secret.
_FIELD = re.compile(r"""(?ix)
    (?P<key>["']?(?P<name>[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|session|cookie)[A-Za-z0-9_.-]*)["']?
     \s*[:=][ \t]*)
    (?P<value>"[^"\n]*"|'[^'\n]*'|[^\s,;&}\]\[{]+)""")
# Names of a reference to a secret, not the secret itself.
_REFERENCE = re.compile(r"(?i)(name|names|ref|refs|keyref|_file|file|path|expir\w*|ttl\w*|seconds|enabled|count)$")
_HARMLESS = {"", '""', "''", "null", "true", "false", REDACTED}


def _field(match: re.Match[str]) -> str:
    if _REFERENCE.search(match.group("name")) or match.group("value") in _HARMLESS:
        return match.group(0)
    return match.group("key") + REDACTED


# Literal environment values in pod specs and Kionga resources (YAML and JSON).
# A value's name says nothing about its content, so every literal is removed and
# only the name is kept. valueFrom references carry no value and survive.
_ENV_YAML = re.compile(r"(?m)^(?P<lead>[ \t]*-?[ \t]*name:[ \t]*[^\n]+\n[ \t]*value:[ \t]*)(?P<value>(?!\[REDACTED\])[^\n]+)$")
_ENV_JSON = re.compile(r'(?P<lead>"name":\s*"[^"]*",\s*"value":\s*)"(?:[^"\\]|\\.)*"')


def redact(text: str) -> str:
    """Remove credentials from free text, logs, JSON and YAML."""
    text = _PATTERNS[0][0].sub(_PATTERNS[0][1], text)
    text = _ENV_YAML.sub(lambda m: m.group("lead") + REDACTED, text)
    text = _ENV_JSON.sub(lambda m: m.group("lead") + '"' + REDACTED + '"', text)
    text = _FIELD.sub(_field, text)
    for pattern, replacement in _PATTERNS[1:]:
        text = pattern.sub(replacement, text)
    return text


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


KIONGA_KINDS = ["kiongaapplications", "kiongafunctions", "kiongamodeldeployments", "kiongaagents", "kiongaworkspaces",
                "kiongamodelpromotions", "kiongatools", "kiongaconnections"]
WORKLOAD_KINDS = ["deployments", "replicasets", "statefulsets", "jobs", "services", "endpoints", "ingresses", "networkpolicies",
                  "poddisruptionbudgets", "horizontalpodautoscalers", "persistentvolumeclaims", "resourcequotas", "configmaps"]
ADDON_KINDS = ["scaledobjects.keda.sh", "httpscaledobjects.http.keda.sh", "interceptorroutes.http.keda.sh",
               "inferenceservices.serving.kserve.io", "certificates.cert-manager.io"]
PREREQUISITE_NAMESPACES = ["cert-manager", "keda", "kserve"]
FORBIDDEN = ("secret", "secrets")


@dataclass
class Bundle:
    files: dict[str, str] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)

    def add(self, path: str, text: str) -> None:
        self.files[path] = redact(text)


class Collector:
    def __init__(self, release: str, namespace: str, context: str = "", since: str = "2h", tail: int = 2000,
                 run: Runner = default_runner):
        self.release, self.namespace, self.context = release, namespace, context
        self.since, self.tail, self.run = since, tail, run
        self.bundle = Bundle()

    def kubectl(self, *args: str) -> list[str]:
        kinds = [token.lower() for arg in args for token in arg.split(",")]
        if any(kind in FORBIDDEN or kind.startswith("secrets.") for kind in kinds):
            raise ValueError("the support bundle never reads Secrets")
        return ["kubectl", *(["--context", self.context] if self.context else []), *args]

    def helm(self, *args: str) -> list[str]:
        return ["helm", *(["--kube-context", self.context] if self.context else []), *args]

    def capture(self, path: str, argv: list[str]) -> str | None:
        try:
            result = self.run(argv)
        except (OSError, subprocess.SubprocessError) as error:
            self.bundle.failures.append({"file": path, "command": " ".join(argv), "error": str(error)})
            return None
        if result.returncode != 0:
            self.bundle.failures.append({"file": path, "command": " ".join(argv), "error": redact(result.stderr.strip())[:2000]})
            return None
        self.bundle.add(path, result.stdout)
        return result.stdout

    def namespaces(self, values: dict) -> list[str]:
        """Kionga's namespaces from the release values, in a stable order."""
        found = [self.namespace, values.get("workspaceNamespace") or "kionga-workloads"]
        if (values.get("pipelineExecutor") or {}).get("kubernetes"):
            found.append(values["pipelineExecutor"].get("namespace") or "kionga-pipelines")
        if (values.get("sandbox") or {}).get("enabled"):
            found.append(values["sandbox"].get("namespace") or "kionga-sandboxes")
        return list(dict.fromkeys(found))

    def collect(self) -> Bundle:
        self.capture("versions/kubectl.txt", self.kubectl("version"))
        self.capture("versions/helm.txt", ["helm", "version", "--short"])
        self.capture("helm/list.txt", self.helm("list", "--all-namespaces"))
        self.capture("helm/status.txt", self.helm("status", self.release, "-n", self.namespace))
        self.capture("helm/history.txt", self.helm("history", self.release, "-n", self.namespace))
        raw_values = self.capture("helm/values.json", self.helm("get", "values", self.release, "-n", self.namespace, "-o", "json"))
        values: dict = {}
        if raw_values:
            try:
                values = json.loads(raw_values) or {}
            except json.JSONDecodeError:
                values = {}
        self.capture("cluster/nodes.txt", self.kubectl("get", "nodes", "-o", "wide"))
        self.capture("cluster/crds.txt", self.kubectl("get", "customresourcedefinitions"))
        for namespace in PREREQUISITE_NAMESPACES:
            self.capture(f"prerequisites/{namespace}/pods.txt", self.kubectl("get", "pods", "-n", namespace, "-o", "wide"))
        for namespace in self.namespaces(values):
            base = f"namespaces/{namespace}"
            self.capture(f"{base}/kionga-resources.yaml",
                         self.kubectl("get", ",".join(KIONGA_KINDS), "-n", namespace, "-o", "yaml", "--ignore-not-found"))
            self.capture(f"{base}/addons.yaml", self.kubectl("get", ",".join(ADDON_KINDS), "-n", namespace, "-o", "yaml", "--ignore-not-found"))
            self.capture(f"{base}/workloads.txt", self.kubectl("get", ",".join(WORKLOAD_KINDS), "-n", namespace, "-o", "wide"))
            self.capture(f"{base}/events.txt", self.kubectl("get", "events", "-n", namespace, "--sort-by=.lastTimestamp"))
            pods = self.capture(f"{base}/pods.json", self.kubectl("get", "pods", "-n", namespace, "-o", "json"))
            for pod, container, restarted in containers(pods):
                path = f"{base}/logs/{pod}/{container}.log"
                self.capture(path, self.kubectl("logs", pod, "-c", container, "-n", namespace, f"--since={self.since}",
                                                f"--tail={self.tail}", "--timestamps"))
                if restarted:
                    self.capture(path.replace(".log", ".previous.log"),
                                 self.kubectl("logs", pod, "-c", container, "-n", namespace, "--previous", f"--tail={self.tail}"))
        return self.bundle


def containers(pods_json: str | None) -> list[tuple[str, str, bool]]:
    """(pod, container, restarted) for every init and app container."""
    if not pods_json:
        return []
    try:
        items = json.loads(pods_json).get("items", [])
    except json.JSONDecodeError:
        return []
    found = []
    for pod in items:
        name = pod["metadata"]["name"]
        statuses = {s["name"]: s.get("restartCount", 0) for s in
                    (pod.get("status", {}).get("containerStatuses") or []) + (pod.get("status", {}).get("initContainerStatuses") or [])}
        spec = pod.get("spec", {})
        for container in (spec.get("initContainers") or []) + (spec.get("containers") or []):
            found.append((name, container["name"], statuses.get(container["name"], 0) > 0))
    return found


def write_tarball(bundle: Bundle, output: Path, meta: dict) -> Path:
    manifest = dict(meta, files=sorted(bundle.files), failures=bundle.failures)
    with tarfile.open(output, "w:gz") as archive:
        for path, text in sorted({**bundle.files, "manifest.json": json.dumps(manifest, indent=2) + "\n"}.items()):
            data = text.encode()
            info = tarfile.TarInfo(f"kionga-support/{path}")
            info.size, info.mode, info.mtime = len(data), 0o600, int(meta["collected_at_epoch"])
            archive.addfile(info, io.BytesIO(data))
    output.chmod(0o600)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", default="")
    parser.add_argument("--namespace", default="kionga-system")
    parser.add_argument("--release", default="kionga")
    parser.add_argument("--since", default="2h", help="log window, e.g. 30m or 2h")
    parser.add_argument("--tail", type=int, default=2000, help="most lines per container")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+[smh]", args.since):
        parser.error("--since takes a duration such as 30m or 2h")
    now = dt.datetime.now(dt.timezone.utc)
    output = args.output or Path(f"kionga-support-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz")
    bundle = Collector(args.release, args.namespace, args.context, args.since, args.tail).collect()
    write_tarball(bundle, output, {"release": args.release, "namespace": args.namespace, "context": args.context,
                                   "collected_at": now.isoformat(), "collected_at_epoch": now.timestamp()})
    print(f"Wrote {output} ({len(bundle.files)} files, {len(bundle.failures)} commands failed; see manifest.json)")
    print("Review it before sending: redaction removes known credential shapes, not every secret a log could contain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
