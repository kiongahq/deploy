"""Idempotent bucket bootstrap for the S3-compatible object store (RustFS by
default), using the boto3 already in MLflow's image."""

import os
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError


BUCKETS = (
    "mlaiops-models",
    "mlaiops-artifacts",
    "mlaiops-features",
    "mlaiops-traces",
    "mlaiops-agents",
    "mlaiops-pipeline-logs",
    "kionga-blog-media",
)
EXISTS_CODES = {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}


def main():
    endpoint = os.environ.get("S3_ENDPOINT") or os.environ["MINIO_ENDPOINT"]
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


if __name__ == "__main__":
    main()
