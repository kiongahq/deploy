#!/usr/bin/env python3
"""Build Compose images with Podman, including the SDK's named build contexts.

Compose's classic Docker API builder cannot send additional_contexts. Podman's
native builder accepts them and preserves local incremental build caching.
"""
import json
import subprocess
import sys
from pathlib import Path


def build_command(name, service, project):
    build = service['build']
    context = Path(build['context'])
    command = ['podman', 'build', '--format', 'docker', '--tag',
               service.get('image') or f'{project}-{name}',
               '--file', str(context / build.get('dockerfile', 'Dockerfile'))]
    for key, value in (build.get('args') or {}).items():
        command += ['--build-arg', key if value is None else f'{key}={value}']
    for key, value in (build.get('additional_contexts') or {}).items():
        command += ['--build-context', f'{key}={value}']
    if build.get('target'):
        command += ['--target', build['target']]
    return [*command, str(context)]


def main():
    base, override, *selected = sys.argv[1:]
    if any(name.startswith('-') for name in selected):
        raise SystemExit('Native Podman build expects service names, without build flags.')
    raw = subprocess.check_output(
        ['podman', 'compose', '-f', base, '-f', override, 'config', '--format', 'json'], text=True)
    config = json.loads(raw)
    services = config['services']
    for name in selected or services:
        service = services[name]
        if service.get('build'):
            print(f'Building {name} with Podman', flush=True)
            subprocess.run(build_command(name, service, config['name']), check=True)


if __name__ == '__main__':
    main()
