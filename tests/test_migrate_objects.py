"""MinIO -> RustFS object migration: copies everything, verifies by size and
MD5, resumes without recopying, never writes to the source, and reports."""

import csv
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("migrate_objects", ROOT / "scripts/migrate_objects.py")
mig = importlib.util.module_from_spec(spec)
sys.modules["migrate_objects"] = mig  # dataclasses resolve their module by name
spec.loader.exec_module(mig)


class FakeS3:
    """Just enough of a boto3 S3 client, backed by dicts."""

    def __init__(self, buckets=None, corrupt=None, multipart=(), multipart_etags=False):
        self.buckets = {b: dict(objs) for b, objs in (buckets or {}).items()}
        self.corrupt = corrupt  # (bucket, key) whose stored bytes get damaged
        self.multipart = set(multipart)
        self.writes = 0
        self.multipart_etags = multipart_etags  # every stored object reports "<md5>-N"
        self.reads = 0

    @staticmethod
    def _etag(data, multipart=False):
        md5 = hashlib.md5(data).hexdigest()  # noqa: S324
        return f'"{md5}-2"' if multipart else f'"{md5}"'

    def list_buckets(self):
        return {"Buckets": [{"Name": b} for b in self.buckets]}

    def get_paginator(self, _name):
        store = self

        class Paginator:
            def paginate(self, Bucket):
                yield {"Contents": [
                    {"Key": k, "Size": len(v["data"]),
                     "ETag": store._etag(v["data"], (Bucket, k) in store.multipart)}
                    for k, v in sorted(store.buckets[Bucket].items())]}
        return Paginator()

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise KeyError(Bucket)

    def create_bucket(self, Bucket):
        self.buckets.setdefault(Bucket, {})

    def head_object(self, Bucket, Key):
        obj = self.buckets[Bucket][Key]
        return {"ContentLength": len(obj["data"]), "ETag": self._etag(obj["data"], self.multipart_etags)}

    def get_object(self, Bucket, Key):
        obj = self.buckets[Bucket][Key]
        self.reads += 1
        return {"Body": io.BytesIO(obj["data"]), "ContentType": obj.get("type"),
                "Metadata": obj.get("meta", {})}

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):
        self.writes += 1
        data = b"".join(iter(lambda: fileobj.read(3), b""))
        if (Bucket, Key) == self.corrupt:
            data = data[:-1] + b"X"
        self.buckets[Bucket][Key] = {"data": data, "type": (ExtraArgs or {}).get("ContentType"),
                                     "meta": (ExtraArgs or {}).get("Metadata", {})}


def source():
    return FakeS3({
        "mlaiops-models": {"m/1/model.pkl": {"data": b"model-bytes", "type": "application/octet-stream",
                                             "meta": {"run": "r1"}}},
        "kionga-blog-media": {"a.png": {"data": b"\x89PNG..", "type": "image/png"},
                              "big.bin": {"data": b"x" * 50}},
        "empty-bucket": {},
    }, multipart={("kionga-blog-media", "big.bin")})


def clock():
    t = [0.0]

    def now():
        t[0] += 1.0
        return t[0]
    return now


def test_copies_every_bucket_and_object_with_metadata(tmp_path):
    src, dst = source(), FakeS3()
    totals = mig.migrate(src, dst, tmp_path, clock())
    assert totals.failed == 0 and totals.copied == 3 and totals.objects == 3
    assert set(dst.buckets) == {"mlaiops-models", "kionga-blog-media", "empty-bucket"}
    model = dst.buckets["mlaiops-models"]["m/1/model.pkl"]
    assert model["data"] == b"model-bytes" and model["meta"] == {"run": "r1"}
    assert dst.buckets["kionga-blog-media"]["a.png"]["type"] == "image/png"
    assert src.writes == 0                       # the source is only read
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["verdict"] == "ok" and summary["bytes"] == 11 + 6 + 50


def test_rerun_resumes_without_copying_again(tmp_path):
    src, dst = source(), FakeS3()
    mig.migrate(src, dst, tmp_path, clock())
    writes = dst.writes
    totals = mig.migrate(src, dst, tmp_path, clock())
    assert totals.skipped == 3 and totals.copied == 0 and dst.writes == writes


def test_same_size_but_different_content_is_recopied(tmp_path):
    src = source()
    dst = FakeS3({"mlaiops-models": {"m/1/model.pkl": {"data": b"MODEL-BYTES"}}})
    totals = mig.migrate(src, dst, tmp_path, clock())
    assert dst.buckets["mlaiops-models"]["m/1/model.pkl"]["data"] == b"model-bytes"
    assert totals.copied == 3


def test_corruption_is_reported_and_fails_the_run(tmp_path):
    src = source()
    dst = FakeS3(corrupt=("mlaiops-models", "m/1/model.pkl"))
    totals = mig.migrate(src, dst, tmp_path, clock())
    assert totals.failed == 1
    rows = list(csv.DictReader((tmp_path / "report.csv").open()))
    bad = next(r for r in rows if r["key"] == "m/1/model.pkl")
    assert bad["status"] == "failed" and "md5" in bad["detail"]
    assert json.loads((tmp_path / "summary.json").read_text())["verdict"] == "failed"


def test_multipart_etag_falls_back_to_size_check(tmp_path):
    assert mig.plain_md5('"0cc175b9c0f1b6a831c399e269772661-2"') is None
    assert mig.plain_md5('"0cc175b9c0f1b6a831c399e269772661"') == "0cc175b9c0f1b6a831c399e269772661"
    totals = mig.migrate(source(), FakeS3(), tmp_path, clock())
    rows = {r["key"]: r for r in csv.DictReader((tmp_path / "report.csv").open())}
    assert rows["big.bin"]["status"] == "copied" and rows["big.bin"]["source_md5"] == ""
    assert totals.failed == 0


def test_progress_log_has_percent_eta_and_counts(tmp_path):
    mig.migrate(source(), FakeS3(), tmp_path, clock())
    lines = (tmp_path / "progress.log").read_text().splitlines()
    assert "minio-to-rustfs" in lines[0] and "start: 3 buckets" in lines[0]
    assert "100.0%" in lines[-1] and "objects 3/3" in lines[-1] and "done: ok" in lines[-1]


def test_multipart_destination_is_verified_by_reading_back(tmp_path):
    """When the destination's ETags are multipart, the stored bytes are read back
    and hashed, so corruption there is still caught."""
    good = FakeS3(multipart_etags=True)
    assert mig.migrate(source(), good, tmp_path, clock()).failed == 0
    assert good.reads == 3
    bad = FakeS3(multipart_etags=True, corrupt=("kionga-blog-media", "big.bin"))
    totals = mig.migrate(source(), bad, tmp_path / "bad", clock())
    rows = {r["key"]: r for r in csv.DictReader((tmp_path / "bad" / "report.csv").open())}
    assert totals.failed == 1 and "stored md5" in rows["big.bin"]["detail"]
