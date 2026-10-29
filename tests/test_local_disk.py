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


def test_cached_only_start_invokes_compose_without_build_or_pull():
    result = run_harness(
        "BUILD_MODE=never; compose() { printf '%s\\n' \"$*\"; }; start_platform"
    )
    assert result.returncode == 0, result.stderr
    assert "up --detach --pull never --remove-orphans --no-build" in result.stdout
    assert "compose build" not in result.stdout


def test_default_start_builds_only_missing_images():
    result = run_harness(
        "compose() { printf 'compose %s\\n' \"$*\"; }"
        "; missing_services() { echo gateway agent-runtime; }; stale_services() { echo SHOULD-NOT-RUN; }"
        "; ensure_build_space() { echo space-checked; }; check_build_context() { :; }; record_build_inputs() { echo recorded \"$*\"; }"
        "; start_platform"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Building images that are not cached yet: gateway agent-runtime", "space-checked",
        "compose build gateway agent-runtime", "recorded gateway agent-runtime",
        "compose up --detach --pull never --remove-orphans --no-build",
    ]


def test_default_start_skips_builds_when_everything_is_cached():
    result = run_harness(
        "compose() { printf 'compose %s\\n' \"$*\"; }; missing_services() { :; }; start_platform"
    )
    assert result.returncode == 0, result.stderr
    assert "build" not in result.stdout.replace("--no-build", "")


def space_harness(host_gib, slack_gib, after_prune_gib=None, prune="1"):
    gib = 1024 * 1024
    after = f"; ENGINE=(fake_engine); fake_engine() {{ echo \"engine $*\" >>\"$CALLS\"; PRUNED=1; }}"
    return run_harness(
        f"export KIONGA_AUTO_PRUNE={prune}; CALLS=$(mktemp); PRUNED=0{after}"
        f"; host_free_kib() {{ echo {host_gib * gib}; }}"
        f"; engine_slack_kib() {{ if [[ $PRUNED == 1 ]]; then echo {(after_prune_gib or slack_gib) * gib}; else echo {slack_gib * gib}; fi; }}"
        "; ensure_build_space && echo enough; cat \"$CALLS\" >&2"
    )


def test_build_space_counts_space_the_engine_already_holds():
    result = space_harness(host_gib=11, slack_gib=29)
    assert result.returncode == 0 and "enough" in result.stdout
    assert "prune" not in result.stderr, "no pruning when the engine already holds enough space"


def test_dangling_images_are_pruned_only_when_space_is_short():
    result = space_harness(host_gib=8, slack_gib=2, after_prune_gib=9)
    assert result.returncode == 0, result.stderr
    assert "engine image prune -f" in result.stderr, "only dangling images: no -a, no volumes"
    result = space_harness(host_gib=8, slack_gib=2, after_prune_gib=3)
    assert result.returncode != 0 and "Container builds need about 15 GiB" in result.stderr
    result = space_harness(host_gib=8, slack_gib=2, prune="0")
    assert result.returncode != 0 and "engine image prune" not in result.stderr


def test_unreachable_podman_machine_is_restarted():
    result = run_harness(
        "export KIONGA_ENGINE=podman; TMPDIR=$(mktemp -d)"
        "; engine_reachable() { [[ -f \"$TMPDIR/restarted\" ]]; }"
        "; CALLS=$(mktemp); podman() { echo \"podman $*\" >>\"$CALLS\"; [[ \"$*\" == 'machine start' ]] && touch \"$TMPDIR/restarted\"; return 0; }"
        "; ENGINE=(podman); recover_engine && echo recovered; cat \"$CALLS\" >&2"
    )
    assert "podman machine stop" in result.stderr and "podman machine start" in result.stderr
    assert "recovered" in result.stdout


def test_engine_recovery_can_be_disabled():
    result = run_harness("export KIONGA_AUTO_RECOVER_ENGINE=0; recover_engine || echo skipped")
    assert result.stdout.strip() == "skipped"


def test_missing_images_report_progress_and_classify_failures():
    script = (
        "compose() { echo '{}'; }; require_free_space() { :; }"
        "; python3() { printf 'a/one:1\\nb/two:2\\n'; }"
        "; CALLS=$(mktemp); ENGINE=(fake); fake() { echo \"$*\" >>\"$CALLS\"; case \"$*\" in"
        " 'image inspect'*) return 1 ;; *two*) echo 'manifest unknown' >&2; return 1 ;; *) return 0 ;; esac; }"
        "; pull_missing_upstream_images && rc=0 || rc=$?; echo rc=$rc; cat \"$CALLS\" >&2"
    )
    result = run_harness(script)
    assert "2 upstream image(s) are not cached" in result.stdout
    assert "[1/2] pulling a/one:1" in result.stdout and "[1/2] done in" in result.stdout
    assert "[2/2] pulling b/two:2" in result.stdout
    assert "rc=2" in result.stdout, "manifest unknown is a permanent failure"
    assert "pull -q a/one:1" in result.stderr, "without a terminal pulls stay quiet"
