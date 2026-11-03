"""Shared test setup.

compose.yaml has no default for the trace proxy's keys, so `compose config` needs
them from the environment, the way local-up.sh and .env provide them on a real
host. These values exist only for rendering configuration; they are not defaults
anywhere. tests/test_trace_proxy_keys.py covers what happens when they are unset."""

import os

os.environ.setdefault("TRACE_PROXY_PROJECT_KEY", "test-only-project-key")
os.environ.setdefault("TRACE_PROXY_KEYS", '[{"key_id":"test","key_sha256":"' + "0" * 64 + '","project":"test"}]')
