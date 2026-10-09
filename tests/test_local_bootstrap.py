"""Guard the small local startup path against removed registry images."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
# Component checkouts sit beside this repo (make workspace), as compose.yaml expects.
SRC = Path(os.environ.get("KIONGA_SRC") or ROOT.parent).resolve()
COMPONENTS = {"platform", "serving", "trace-proxy", "agent-runtime", "pipeline-runner",
              "sdk-python", "workspace"}


def component(name: str) -> Path:
    """A sibling checkout; missing ones fail loudly instead of skipping."""
    path = SRC / name
    assert (path / ".git").exists(), f"{path} is missing: run 'make workspace' (or set KIONGA_SRC)"
    return path


def compose_config(*profiles: str) -> dict:
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for config validation")
    args = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
    for profile in profiles:
        args += ["--profile", profile]
    result = subprocess.run([*args, "config", "--format", "json"], cwd=ROOT,
                            capture_output=True, text=True, check=True,
                            env={**os.environ, "KIONGA_SRC": str(SRC)})
    return json.loads(result.stdout)


def test_compose_reuses_existing_images_for_bucket_init_and_materializer():
    config = compose_config()
    services = config["services"]
    assert services["objectstore-init"]["image"] == services["mlflow"]["image"]
    assert services["feature-materializer"]["image"] == services["agent-runtime"]["image"]
    assert "minio/mc" not in json.dumps(config)


def test_ide_and_gateway_builds_use_small_contexts():
    services = compose_config("ide")["services"]
    assert services["ide"]["build"]["context"] == str(SRC / "workspace")
    assert services["ide"]["build"]["args"]["INSTALL_CODING_AGENTS"] == "false"
    assert services["gateway"]["build"]["context"] == str(SRC / "platform" / "go")


def test_every_build_comes_from_a_component_repo():
    """Each image builds from one sibling repository (or this repo's own mlflow
    image), and its Dockerfile and named contexts exist there."""
    for name, svc in compose_config("ide")["services"].items():
        build = svc.get("build")
        if not build:
            continue
        context = Path(build["context"])
        # The one image built from this repo. SRC can be this repo's parent
        # (local) or sit inside it (CI clones components into ./src), so match
        # it exactly rather than by prefix.
        if context != ROOT / "mlflow":
            assert context.is_relative_to(SRC), (name, context)
            repo = context.relative_to(SRC).parts[0]
            assert repo in COMPONENTS, (name, repo)
            component(repo)
        assert (context / build.get("dockerfile", "Dockerfile")).is_file(), name
        for extra in (build.get("additional_contexts") or {}).values():
            assert Path(extra).relative_to(SRC).parts[0] in COMPONENTS, (name, extra)
            assert (Path(extra) / ".git").exists(), (name, extra)


def _load_objectstore_init():
    spec = importlib.util.spec_from_file_location("objectstore_init", ROOT / "objectstore-init.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("exists_code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_bucket_bootstrap_is_idempotent(monkeypatch, exists_code):
    """Both codes S3 stores use for "you already have it" are accepted (RustFS
    and MinIO differ), and every platform bucket is requested."""
    pytest.importorskip("boto3")
    module = _load_objectstore_init()
    created, endpoints = [], []

    class Client:
        def list_buckets(self):
            return {"Buckets": []}

        def create_bucket(self, *, Bucket):
            created.append(Bucket)
            if Bucket == module.BUCKETS[0]:
                raise module.ClientError(
                    {"Error": {"Code": exists_code, "Message": "exists"}}, "CreateBucket")

    monkeypatch.setenv("S3_ENDPOINT", "http://objectstore:9000")
    monkeypatch.setattr(module.boto3, "client",
                        lambda *_, endpoint_url, **__: endpoints.append(endpoint_url) or Client())
    module.main()
    assert tuple(created) == module.BUCKETS
    assert endpoints == ["http://objectstore:9000"]


def test_bucket_bootstrap_rejects_other_errors(monkeypatch):
    pytest.importorskip("boto3")
    module = _load_objectstore_init()

    class Client:
        def list_buckets(self):
            return {"Buckets": []}

        def create_bucket(self, *, Bucket):
            raise module.ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateBucket")

    monkeypatch.setenv("S3_ENDPOINT", "http://objectstore:9000")
    monkeypatch.setattr(module.boto3, "client", lambda *_, **__: Client())
    with pytest.raises(module.ClientError):
        module.main()


def test_object_store_is_pinned_rustfs_and_every_client_uses_it():
    """MinIO images are no longer pullable; RustFS is pinned by digest, keeps the
    `minio` alias for older settings, and every S3 client points at it."""
    services = compose_config("ide")["services"]
    store = services["objectstore"]
    assert store["image"].startswith("rustfs/rustfs:1.0.1@sha256:")
    assert not [n for n, s in services.items() if "minio/" in s.get("image", "")]
    assert "minio" in store["networks"]["default"]["aliases"]
    assert store["healthcheck"]["test"][-1].endswith("/health")
    assert services["objectstore-init"]["depends_on"]["objectstore"]["condition"] == "service_healthy"
    endpoint_vars = {"MLFLOW_S3_ENDPOINT_URL", "S3_ENDPOINT", "KIONGA_BLOG_MEDIA_S3_ENDPOINT"}
    endpoints = {(n, k): v for n, s in services.items()
                 for k, v in (s.get("environment") or {}).items() if k in endpoint_vars}
    assert endpoints, "no S3 clients found"
    assert set(endpoints.values()) == {"http://objectstore:9000"}, endpoints


def _load_build_inputs():
    spec = importlib.util.spec_from_file_location("build_inputs", ROOT / "scripts/build_inputs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jupyter_base_url_matches_gateway_proxy_prefix():
    """The gateway proxies and probes /workspaces/workbench/ unchanged."""
    entrypoint = (component("workspace") / "jupyter/entrypoint.sh").read_text()
    workspaces = (component("platform") / "go/internal/httpapi/workspaces.go").read_text()
    assert "--ServerApp.base_url=/workspaces/workbench/" in entrypoint
    assert '"/workspaces/workbench/api"' in workspaces
    compose = (ROOT / "compose.yaml").read_text()
    assert "/workspaces/workbench/api/status" in compose


def test_copy_sources_ignore_stage_copies(tmp_path):
    module = _load_build_inputs()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.11 AS base\n"
        "COPY --chown=dev python/a python/b /opt/\n"
        "COPY --from=base /x /y\n"
        "ADD deploy/entry.sh \\\n    /usr/local/bin/entry\n"
    )
    assert module.copy_sources(dockerfile) == ["python/a", "python/b", "deploy/entry.sh"]


def test_stale_detects_changed_copy_input_and_new_image(tmp_path):
    """Regression: a cached image must not survive an entrypoint change."""
    module = _load_build_inputs()
    (tmp_path / "deploy").mkdir()
    entry = tmp_path / "deploy/entrypoint.sh"
    entry.write_text("jupyter lab\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\nCOPY deploy/entrypoint.sh /bin/entry\n")
    config = {"services": {
        "jupyter": {"image": "kionga-jupyter", "build": {"context": str(tmp_path), "dockerfile": "Dockerfile"}},
        "redis": {"image": "redis:7"},
    }}
    ids = {"kionga-jupyter": "sha256:one"}
    lookup = lambda image: ids.get(image, "")  # noqa: E731

    assert module.stale(config, {}, lookup) == ["jupyter"]
    state = module.record(config, {}, ["jupyter"], lookup)
    assert module.stale(config, state, lookup) == []

    entry.write_text("jupyter lab --ServerApp.base_url=/workspaces/workbench/\n")
    assert module.stale(config, state, lookup) == ["jupyter"]

    state = module.record(config, state, ["jupyter"], lookup)
    ids["kionga-jupyter"] = "sha256:two"
    assert module.stale(config, state, lookup) == ["jupyter"]
    ids.pop("kionga-jupyter")
    assert module.stale(config, state, lookup) == ["jupyter"]


def test_named_context_change_marks_image_stale(tmp_path):
    """Regression: an SDK edit must rebuild images that install it through a
    named build context (COPY --from=sdk), not only the image's own sources."""
    module = _load_build_inputs()
    app, sdk = tmp_path / "app", tmp_path / "sdk"
    app.mkdir(); sdk.mkdir()
    (app / "Dockerfile").write_text("FROM scratch AS sdk\nFROM python:3.11\nCOPY --from=sdk / /opt/sdk/\n")
    (sdk / "client.py").write_text("v = 1\n")
    config = {"services": {"agent": {"image": "agent", "build": {
        "context": str(app), "dockerfile": "Dockerfile", "additional_contexts": {"sdk": str(sdk)}}}}}
    lookup = lambda image: "sha256:one"  # noqa: E731
    state = module.record(config, {}, ["agent"], lookup)
    assert module.stale(config, state, lookup) == []
    (sdk / "client.py").write_text("v = 2\n")
    assert module.stale(config, state, lookup) == ["agent"]


def test_build_inputs_cover_every_compose_build():
    module = _load_build_inputs()
    config = compose_config("ide")
    built = module.built_services(config)
    assert {"gateway", "jupyter", "ide"} <= set(built)
    for name, svc in built.items():
        assert len(module.service_hash(svc["build"])) == 64, name
