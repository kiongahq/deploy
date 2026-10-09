#!/usr/bin/env python3
"""Copy every bucket and object from one S3 endpoint to another, then verify.

Used to move a Kionga MinIO volume into RustFS (scripts/migrate-minio-to-rustfs.sh).
The source is only read. Each object's bytes are hashed while streaming; an
object counts as verified when the destination stores the same size and the same
MD5 (from its ETag, or by reading the object back when the ETag is multipart),
and that MD5 matches the source ETag whenever the source ETag is a plain MD5. Objects already
present at the destination with the same size (and the same MD5 when both sides
report one) are skipped, so a re-run resumes.
Only the current version of each object is copied.

Writes to --out (default /tmp/minio-to-rustfs):
  progress.log   timestamped progress: percent, ETA, rate, errors
  report.csv     one row per object: bucket, key, size, md5, before, after, status
  summary.json   totals per bucket and the verdict

Env: SOURCE_ENDPOINT, SOURCE_ACCESS_KEY, SOURCE_SECRET_KEY,
     DEST_ENDPOINT, DEST_ACCESS_KEY, DEST_SECRET_KEY
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CHUNK = 8 * 1024 * 1024


@dataclass
class Totals:
    objects: int = 0
    bytes: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    per_bucket: dict = field(default_factory=dict)


class HashingReader:
    """File-like wrapper that hashes and counts what the uploader reads."""

    def __init__(self, body):
        self.body = body
        self.md5 = hashlib.md5()  # noqa: S324 (integrity check, not security)
        self.size = 0

    def read(self, amount=-1):
        data = self.body.read(CHUNK if amount is None or amount < 0 else amount)
        self.md5.update(data)
        self.size += len(data)
        return data


def plain_md5(etag: str) -> str | None:
    """The MD5 inside an S3 ETag, or None for a multipart ETag ("<md5>-<parts>")."""
    etag = (etag or "").strip('"')
    return None if "-" in etag or len(etag) != 32 else etag


def list_objects(client, bucket):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        yield from page.get("Contents", [])


def dest_head(client, bucket, key):
    """(size, plain md5 or None) at the destination, or (None, None) if absent."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 (absent or unreadable both mean "copy it")
        return None, None
    return head["ContentLength"], plain_md5(head.get("ETag", ""))


def stored_md5(client, bucket, key):
    """MD5 of what the destination actually stores: its ETag when that is a
    plain MD5, otherwise the bytes read back."""
    _, etag_md5 = dest_head(client, bucket, key)
    if etag_md5:
        return etag_md5
    digest = hashlib.md5()  # noqa: S324 (integrity check, not security)
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def ensure_bucket(client, bucket):
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001
        client.create_bucket(Bucket=bucket)


class Progress:
    def __init__(self, path: Path, total_objects: int, total_bytes: int, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.total_objects, self.total_bytes = total_objects, total_bytes
        self.start = self.last = clock()

    def emit(self, done_objects, done_bytes, failed, note="", force=False):
        now = self.clock()
        if not force and now - self.last < 30 and done_objects % 500:
            return
        self.last = now
        pct = 100.0 if not self.total_bytes else 100.0 * done_bytes / self.total_bytes
        elapsed = max(now - self.start, 1e-9)
        rate = done_bytes / elapsed
        eta = (self.total_bytes - done_bytes) / rate if rate else float("inf")
        line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')} minio-to-rustfs {pct:5.1f}% "
                f"eta {'n/a' if eta == float('inf') else f'{eta:.0f}s'} "
                f"objects {done_objects}/{self.total_objects} "
                f"bytes {done_bytes}/{self.total_bytes} rate {rate / 1e6:.1f} MB/s "
                f"errors {failed}{' ' + note if note else ''}")
        print(line, flush=True)
        with self.path.open("a") as log:
            log.write(line + "\n")


def migrate(source, dest, out: Path, clock=time.monotonic) -> Totals:
    out.mkdir(parents=True, exist_ok=True)
    buckets = sorted(b["Name"] for b in source.list_buckets().get("Buckets", []))
    inventory = {b: list(list_objects(source, b)) for b in buckets}
    totals = Totals()
    total_objects = sum(len(v) for v in inventory.values())
    total_bytes = sum(o["Size"] for v in inventory.values() for o in v)
    progress = Progress(out / "progress.log", total_objects, total_bytes, clock)
    progress.emit(0, 0, 0, f"start: {len(buckets)} buckets", force=True)

    with (out / "report.csv").open("w", newline="") as handle:
        report = csv.writer(handle)
        report.writerow(["bucket", "key", "size", "source_md5", "dest_size_before",
                         "dest_size_after", "status", "detail"])
        for bucket in buckets:
            ensure_bucket(dest, bucket)
            stats = totals.per_bucket.setdefault(
                bucket, {"objects": 0, "bytes": 0, "copied": 0, "skipped": 0, "failed": 0})
            for obj in inventory[bucket]:
                key, size = obj["Key"], obj["Size"]
                expected = plain_md5(obj.get("ETag", ""))
                before, before_md5 = dest_head(dest, bucket, key)
                status, detail, after = "copied", "", before
                # Resume: already there with the same size, and the same MD5
                # whenever both sides report one.
                if before == size and (not expected or not before_md5 or before_md5 == expected):
                    status = "skipped"
                else:
                    try:
                        got = source.get_object(Bucket=bucket, Key=key)
                        reader = HashingReader(got["Body"])
                        extra = {"Metadata": got.get("Metadata") or {}}
                        if got.get("ContentType"):
                            extra["ContentType"] = got["ContentType"]
                        dest.upload_fileobj(reader, bucket, key, ExtraArgs=extra)
                        after, _ = dest_head(dest, bucket, key)
                        sent = reader.md5.hexdigest()
                        if reader.size != size or after != size:
                            status, detail = "failed", f"size source={size} read={reader.size} dest={after}"
                        elif expected and sent != expected:
                            status, detail = "failed", f"read md5 {sent} != source etag {expected}"
                        elif (stored := stored_md5(dest, bucket, key)) != sent:
                            status, detail = "failed", f"stored md5 {stored} != sent md5 {sent}"
                    except Exception as error:  # noqa: BLE001 (recorded per object, run continues)
                        status, detail = "failed", f"{type(error).__name__}: {error}"
                setattr(totals, status, getattr(totals, status) + 1)
                stats[status] += 1
                stats["objects"] += 1
                stats["bytes"] += size
                totals.objects += 1
                totals.bytes += size
                report.writerow([bucket, key, size, expected or "", before, after, status, detail])
                progress.emit(totals.objects, totals.bytes, totals.failed)

    verdict = "ok" if totals.failed == 0 else "failed"
    progress.emit(totals.objects, totals.bytes, totals.failed, f"done: {verdict}", force=True)
    summary = {"verdict": verdict, "buckets": len(buckets), "objects": totals.objects,
               "bytes": totals.bytes, "copied": totals.copied, "skipped": totals.skipped,
               "failed": totals.failed, "per_bucket": totals.per_bucket}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return totals


def client(prefix):
    import boto3  # deferred so tests run without boto3
    from botocore.config import Config

    return boto3.client(
        "s3", endpoint_url=os.environ[f"{prefix}_ENDPOINT"],
        aws_access_key_id=os.environ[f"{prefix}_ACCESS_KEY"],
        aws_secret_access_key=os.environ[f"{prefix}_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/minio-to-rustfs"))
    args = parser.parse_args(argv)
    totals = migrate(client("SOURCE"), client("DEST"), args.out)
    return 0 if totals.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
