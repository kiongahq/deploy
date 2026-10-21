"""make local-up moves a busy host port instead of failing to start.

Ports bind to loopback by default, so a native Postgres or Redis on
127.0.0.1 now collides with the stack (it used to be shadowed silently).
choose_ports in scripts/local-up.sh keeps free ports, moves busy defaults to
<default>+10000, never hands two services the same port, and refuses to move
a port the operator set explicitly."""

import json
import re
import socket
import subprocess

import pytest
import yaml

from test_sso import ROOT

SCRIPT = ROOT / "scripts" / "local-up.sh"


def table():
    body = re.search(r"PUBLISHED_PORTS=\((.*?)\n\)", SCRIPT.read_text(), re.S).group(1)
    rows = [line.strip().strip('"').split() for line in body.strip().splitlines()]
    return [(service, int(target), var, int(default)) for service, target, var, default in rows]


def test_table_covers_every_default_published_port():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    expected = set()
    for name, service in services.items():
        if service.get("profiles"):
            continue  # opt-in services are not started by local-up
        for port in service.get("ports", []):
            match = re.search(r"\$\{([A-Z_]+_PORT):-(\d+)\}:(\d+)", port)
            assert match, f"{name} publishes {port!r} without a *_PORT variable"
            expected.add((name, int(match.group(3)), match.group(1), int(match.group(2))))
    assert set(table()) == expected


def choose(busy, env=None, existing=None):
    """Run choose_ports with compose and the port probe stubbed; return the
    exported variables and the script's output."""
    rendered = {"services": {}}
    for service, target, var, default in table():
        published = (env or {}).get(var, default)
        rendered["services"].setdefault(service, {"ports": []})["ports"].append(
            {"target": target, "published": str(published)})
    harness = f"""
        source {SCRIPT}
        compose() {{
          case "$1" in
            config) printf '%s' '{json.dumps(rendered)}' ;;
            port) case "$2:$3" in {' '.join(f'"{k}") echo "127.0.0.1:{v}" ;;' for k, v in (existing or {}).items())} *) return 1 ;; esac ;;
          esac
        }}
        port_available() {{ [[ " {' '.join(map(str, busy))} " != *" $1 "* ]]; }}
        choose_ports
        env | grep -E '_PORT=' | sort || true
    """
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **(env or {})})
    exported = dict(line.split("=", 1) for line in result.stdout.splitlines() if re.fullmatch(r"[A-Z_]+_PORT=\d+", line))
    return result.returncode, exported, result.stdout + result.stderr


def test_free_ports_are_kept():
    code, exported, _ = choose(busy=[])
    assert code == 0 and not exported


def test_busy_defaults_move_without_colliding():
    # 9000 + 10000 is agent-runtime's default 19000; the objectstore must not take it.
    code, exported, output = choose(busy=[5432, 6379, 9000])
    assert code == 0, output
    assert exported["POSTGRES_PORT"] == "15432" and exported["REDIS_PORT"] == "16379"
    assert exported["OBJECTSTORE_PORT"] == "19001"
    assert "AGENT_RUNTIME_PORT" not in exported  # its free default is untouched
    ports = [int(v) for v in exported.values()]
    assert len(ports) == len(set(ports))
    assert "Port 5432 is in use; postgres uses 15432 instead" in output


def test_running_stack_keeps_its_ports():
    code, exported, output = choose(busy=[5432, 15432], existing={"postgres:5432": 15432})
    assert code == 0, output
    assert exported["POSTGRES_PORT"] == "15432"


def test_explicit_port_is_never_moved():
    code, _, output = choose(busy=[25432], env={"POSTGRES_PORT": "25432"})
    assert code != 0
    assert "Set POSTGRES_PORT to a free port" in output


def test_port_probe_sees_a_loopback_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        probe = f"source {SCRIPT}; port_available {port}"
        assert subprocess.run(["bash", "-c", probe]).returncode != 0
    finally:
        listener.close()
    assert subprocess.run(["bash", "-c", probe]).returncode == 0


@pytest.mark.parametrize("service, target, var, default", table())
def test_fallback_range_is_valid(service, target, var, default):
    assert default + 10004 <= 65535
