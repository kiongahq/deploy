"""Local startup keeps container build activity off an almost-full host."""

import subprocess

from test_sso import ROOT


SCRIPT = ROOT / "scripts" / "local-up.sh"


def run_harness(command: str):
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {command}'],
        capture_output=True, text=True,
    )


def test_container_build_refuses_low_host_disk():
    result = run_harness(
        "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n'"
        "; printf '/dev/test 10000000 4000000 6291456 40%% /\\n'; }"
        "; require_free_space 'Container builds' 15 KIONGA_MIN_BUILD_FREE_GB"
    )
    assert result.returncode != 0
    assert "require at least 15 GiB free" in result.stderr


def test_cached_start_invokes_compose_without_build_or_pull():
    result = run_harness(
        "BUILD_MODE=never; compose() { printf '%s\\n' \"$*\"; }; start_platform"
    )
    assert result.returncode == 0, result.stderr
    assert "up --detach --pull never --remove-orphans --no-build" in result.stdout
    assert "compose build" not in result.stdout
