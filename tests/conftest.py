"""Shared test setup.

compose.yaml has no default for PostgreSQL, the trace proxy's keys or the model reader, so `compose config` needs
them from the environment, the way local-up.sh and .env provide them on a real
host. These values exist only for rendering configuration; they are not defaults
anywhere. tests/test_trace_proxy_keys.py covers what happens when they are unset."""

import os

# Always override an inherited database credential: tests only render configuration.
os.environ["POSTGRES_PASSWORD"] = "test-only-postgres-credential-for-config-rendering"

os.environ.setdefault("TRACE_PROXY_PROJECT_KEY", "test-only-project-key")
os.environ.setdefault("TRACE_PROXY_KEYS", '[{"key_id":"test","key_sha256":"' + "0" * 64 + '","project":"test"}]')
os.environ.setdefault("SERVING_OBJECTSTORE_ACCESS_KEY", "test-only-reader-access")
os.environ.setdefault("SERVING_OBJECTSTORE_SECRET_KEY", "test-only-reader-secret")
