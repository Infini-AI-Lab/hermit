"""Docker launch policy around the unchanged OfficeQA acting-agent command.

Only networking and the model relay mounts are supplied here. The original
image, user, resources, workspace, Codex arguments and tools remain intact.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

VALUE_OPTIONS = {
    '--name', '--label', '--network', '--tmpfs', '--pids-limit', '--memory',
    '--cap-drop', '--cap-add', '--security-opt', '--user', '--mount', '-w',
    '--workdir', '-e', '--env',
}
FLAG_OPTIONS = {'--rm', '--read-only', '-i', '--interactive', '-t', '--tty', '--init'}


def command(args: list[str], runtime: dict) -> list[str]:
    """Fail closed on an unknown launch shape; never silently keep a network."""
    docker = runtime['docker_bin']
    if not args or args[0] != 'run':
        return [docker, *args]
    options = []
    i = 1
    while i < len(args) and args[i].startswith('-'):
        option, sep, inline = args[i].partition('=')
        if option in FLAG_OPTIONS and not sep:
            options.append(option)
            i += 1
            continue
        if option not in VALUE_OPTIONS:
            raise ValueError('unsupported Docker launch option: ' + option)
        if sep:
            value = inline
            i += 1
        else:
            if i + 1 >= len(args):
                raise ValueError('missing Docker launch option value')
            value = args[i + 1]
            i += 2
        if option == '--network':
            continue
        if option == '--cap-add' and value.upper() != 'SYS_ADMIN':
            raise ValueError('only the original bubblewrap SYS_ADMIN capability is permitted')
        if option == '--mount':
            parts = dict(part.split('=', 1) for part in value.split(',') if '=' in part)
            source = Path(parts.get('src', parts.get('source', '/'))).resolve()
            if parts.get('type') != 'bind' or source == Path('/') or source.name == 'docker.sock' or any(
                    source == Path(prefix) or Path(prefix) in source.parents for prefix in ('/proc', '/sys', '/dev')):
                raise ValueError('host control paths cannot be mounted in an acting container')
        options.extend([option, value])
    if i >= len(args) or args[i] != runtime['docker_image']:
        raise ValueError('the original OfficeQA image is required')
    image, original = args[i], args[i + 1:]
    if not original:
        raise ValueError('an explicit original container command is required')
    options += ['--pull', 'never', '--network', 'none',
        '--mount', f'type=bind,src={runtime["socket"]},dst=/hermit-model.sock,readonly',
        '--mount', f'type=bind,src={runtime["relay"]},dst=/hermit-model-relay.py,readonly',
        '--entrypoint', 'python3']
    return [docker, 'run', *options, image, '/hermit-model-relay.py',
            '--forward', f'{runtime["model_port"]}=/hermit-model.sock', '--', *original]


def main():
    runtime = json.loads(Path(os.environ['HERMIT_COEVOSKILLS_RUNTIME']).read_text())
    argv = command(sys.argv[1:], runtime)
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
