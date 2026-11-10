"""The serving manager downloads model artifacts with a read-only object-store
user, created by objectstore-init.py, instead of the store's root user."""

import importlib.util
import json
import re
import shutil

import pytest
from botocore.exceptions import ClientError
from test_sso import PUBLIC_ENV, ROOT, compose_config

spec = importlib.util.spec_from_file_location("objectstore_init", ROOT / "objectstore-init.py")
init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init)

READER_VARS = ("SERVING_OBJECTSTORE_ACCESS_KEY", "SERVING_OBJECTSTORE_SECRET_KEY")
needs_compose = pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose is required for config validation")


class FakeS3:
    def __init__(self, deny_writes=True):
        self.deny_writes, self.calls = deny_writes, []

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs))

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.deny_writes:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))


def run(reader):
    calls, root = [], FakeS3()
    init.ensure_model_reader("http://store", root, lambda *args: calls.append(args), "reader-id", "reader-secret", reader=reader)
    return calls, root


def test_policy_only_reads_the_model_buckets():
    [statement] = init.READER_POLICY["Statement"]
    assert statement["Effect"] == "Allow"
    assert sorted(statement["Action"]) == ["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket"]
    assert sorted(statement["Resource"]) == ["arn:aws:s3:::kionga-artifacts", "arn:aws:s3:::kionga-artifacts/*",
                                             "arn:aws:s3:::kionga-models", "arn:aws:s3:::kionga-models/*"]
    assert set(init.READER_BUCKETS) <= set(init.BUCKETS)


def test_creates_policy_then_user_then_attaches():
    calls, _ = run(FakeS3())
    assert [call[0] for call in calls] == ["add-canned-policy", "add-user", "set-user-or-group-policy"]
    assert json.loads(calls[1][2]) == {"secretKey": "reader-secret", "status": "enabled"}
    assert calls[2][1] == {"policyName": init.READER_POLICY_NAME, "userOrGroup": "reader-id", "isGroup": "false"}


def test_refuses_a_reader_that_can_write_and_removes_the_probe():
    root = FakeS3()
    with pytest.raises(RuntimeError, match="can write"):
        init.ensure_model_reader("http://store", root, lambda *args: None, "id", "secret", reader=FakeS3(deny_writes=False))
    assert root.calls == [("delete", {"Bucket": "kionga-models", "Key": init.READER_CHECK_KEY})]


def test_other_write_failures_are_not_mistaken_for_read_only():
    class Broken(FakeS3):
        def put_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")

    with pytest.raises(ClientError):
        run(Broken())


def test_admin_errors_do_not_leak_the_body(monkeypatch):
    import urllib.error
    from botocore.credentials import Credentials

    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError("http://store", 403, "denied", {}, None)

    monkeypatch.setattr(init.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError) as failure:
        init.admin_put("http://store", Credentials("root", "root-secret"), "add-user", {"accessKey": "id"}, b'{"secretKey":"reader-secret"}')
    assert "403" in str(failure.value) and "reader-secret" not in str(failure.value)


@needs_compose
def test_serving_manager_uses_the_reader_not_the_root_user():
    services = compose_config("compose.yaml", env={"OBJECTSTORE_ACCESS_KEY": "root-id", "OBJECTSTORE_SECRET_KEY": "root-secret",
                                                   "SERVING_OBJECTSTORE_ACCESS_KEY": "reader-id", "SERVING_OBJECTSTORE_SECRET_KEY": "reader-secret"})
    env = services["serving-manager"]["environment"]
    assert env["AWS_ACCESS_KEY_ID"] == "reader-id" and env["AWS_SECRET_ACCESS_KEY"] == "reader-secret"
    assert services["serving-manager"]["depends_on"]["objectstore-init"]["condition"] == "service_completed_successfully"
    bootstrap = services["objectstore-init"]["environment"]
    assert bootstrap["AWS_SECRET_ACCESS_KEY"] == "root-secret", "only the bootstrap job holds the root user"
    assert bootstrap["SERVING_OBJECTSTORE_ACCESS_KEY"] == "reader-id"


def test_no_default_for_the_reader():
    for name in ("compose.yaml", "compose.public.yaml"):
        text = (ROOT / name).read_text()
        for var in READER_VARS:
            assert not re.search(r"\$\{" + var + r":?-", text), f"{var} must not have a default in {name}"


@needs_compose
def test_public_mode_passes_operator_reader_through():
    env = compose_config("compose.yaml", "compose.public.yaml", env={**PUBLIC_ENV, "SERVING_OBJECTSTORE_ACCESS_KEY": "ops-id", "SERVING_OBJECTSTORE_SECRET_KEY": "ops-secret"})["serving-manager"]["environment"]
    assert env["AWS_ACCESS_KEY_ID"] == "ops-id" and env["AWS_SECRET_ACCESS_KEY"] == "ops-secret"
