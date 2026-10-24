"""Podman configuration keeps socket access limited to engine-control services."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_podman_socket_access_does_not_disable_confinement_for_other_services():
    if not shutil.which('docker'):
        pytest.skip('Compose CLI is required for configuration validation')
    result = subprocess.run(
        ['docker', 'compose', '-f', str(ROOT / 'compose.yaml'),
         '-f', str(ROOT / 'compose.podman.yaml'), 'config', '--format', 'json'],
        env={**os.environ, 'KIONGA_ENGINE_SOCKET': '/run/podman/podman.sock'},
        check=True, capture_output=True, text=True,
    )
    services = json.loads(result.stdout)['services']
    controllers = {'pipeline-runner', 'serving-manager'}
    for name, service in services.items():
        options = service.get('security_opt', [])
        assert ('label=disable' in options) == (name in controllers)
        sockets = [v for v in service.get('volumes', [])
                   if v['target'] == '/var/run/docker.sock']
        if name in controllers:
            assert len(sockets) == 1
            assert sockets[0]['source'] == '/run/podman/podman.sock'
        else:
            assert not sockets


def test_native_build_preserves_named_contexts_and_build_arguments():
    import importlib.util
    spec = importlib.util.spec_from_file_location('podman_build', ROOT / 'scripts/podman-build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = module.build_command('jupyter', {
        'build': {
            'context': '/src/workspace',
            'dockerfile': 'jupyter/Dockerfile',
            'args': {'PLATFORM_SOURCE': 'local'},
            'additional_contexts': {'sdk': '/src/sdk-python'},
        },
    }, 'mlaiops')
    assert command[command.index('--tag') + 1] == 'mlaiops-jupyter'
    assert command[command.index('--file') + 1] == '/src/workspace/jupyter/Dockerfile'
    assert command[command.index('--build-context') + 1] == 'sdk=/src/sdk-python'
    assert command[command.index('--build-arg') + 1] == 'PLATFORM_SOURCE=local'
    assert command[-1] == '/src/workspace'
