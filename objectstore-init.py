"""Idempotent bootstrap for the S3-compatible object store (RustFS by default),
using the boto3 already in MLflow's image: the platform buckets, and a read-only
user for the serving manager, which passes its credentials to artifact downloads."""

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, EndpointConnectionError


BUCKETS = (
    "kionga-models",
    "kionga-artifacts",
    "kionga-features",
    "kionga-traces",
    "kionga-agents",
    "kionga-pipeline-logs",
    "kionga-blog-media",
)
EXISTS_CODES = {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}
# Where MLflow stores model artifacts; nothing else is readable by the reader.
READER_BUCKETS = ("kionga-models", "kionga-artifacts")
READER_POLICY_NAME = "kionga-model-reader"
READER_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
        "Resource": [arn for bucket in READER_BUCKETS for arn in (f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*")],
    }],
}
READER_CHECK_KEY = ".kionga-reader-write-check"


def admin_put(endpoint, credentials, path, query, body=b""):
    """One signed PUT to the store's admin API. Errors never include the body."""
    url = f"{endpoint.rstrip('/')}/rustfs/admin/v3/{path}?{urllib.parse.urlencode(query)}"
    signed = AWSRequest(method="PUT", url=url, data=body, headers={"X-Amz-Content-SHA256": hashlib.sha256(body).hexdigest()})
    S3SigV4Auth(credentials, "s3", "us-east-1").add_auth(signed)
    request = urllib.request.Request(url, data=body, method="PUT", headers=dict(signed.headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # endpoint is operator configuration
            response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"object store admin call {path} returned {error.code}") from None


def ensure_model_reader(endpoint, client, admin, access_key, secret_key, reader=None):
    """Create or update the read-only user, then prove it cannot write."""
    admin("add-canned-policy", {"name": READER_POLICY_NAME}, json.dumps(READER_POLICY).encode())
    admin("add-user", {"accessKey": access_key}, json.dumps({"secretKey": secret_key, "status": "enabled"}).encode())
    admin("set-user-or-group-policy", {"policyName": READER_POLICY_NAME, "userOrGroup": access_key, "isGroup": "false"})
    reader = reader or boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                                    aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    reader.list_objects_v2(Bucket=READER_BUCKETS[0], MaxKeys=1)
    try:
        reader.put_object(Bucket=READER_BUCKETS[0], Key=READER_CHECK_KEY, Body=b"")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "AccessDenied":
            raise
    else:
        client.delete_object(Bucket=READER_BUCKETS[0], Key=READER_CHECK_KEY)
        raise RuntimeError("the model reader can write to the object store; refusing to continue")
    print(f"Model reader can read {', '.join(READER_BUCKETS)} and cannot write", flush=True)


def main():
    endpoint = os.environ["S3_ENDPOINT"]
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    deadline = time.monotonic() + 120
    while True:
        try:
            client.list_buckets()
            break
        except EndpointConnectionError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{endpoint} did not accept connections within 120 seconds") from None
            time.sleep(2)
    for bucket in BUCKETS:
        try:
            client.create_bucket(Bucket=bucket)
            print(f"Created bucket {bucket}", flush=True)
        except ClientError as exc:
            # S3 implementations differ on which code they return for a bucket
            # the caller already owns.
            if exc.response.get("Error", {}).get("Code") not in EXISTS_CODES:
                raise
            print(f"Bucket {bucket} already exists", flush=True)


def bootstrap_model_reader():
    endpoint = os.environ["S3_ENDPOINT"]
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    ensure_model_reader(endpoint, client, lambda *args: admin_put(endpoint, credentials, *args),
                        os.environ["SERVING_OBJECTSTORE_ACCESS_KEY"], os.environ["SERVING_OBJECTSTORE_SECRET_KEY"])


if __name__ == "__main__":
    main()
    bootstrap_model_reader()
