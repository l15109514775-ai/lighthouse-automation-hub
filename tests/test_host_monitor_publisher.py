"""Exercise the root readout with synthetic Docker output, never a live daemon."""
import ast
import json
import os
from pathlib import Path
import subprocess

import pytest


def execute(tmp_path, monkeypatch, docker):
    source = Path(__file__).resolve().parents[1] / 'scripts/host-monitor.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    # Relocate only the fixed output root; execute the real collection/write code.
    roots = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'root' for t in n.targets)]
    assert len(roots) == 1
    roots[0].value = ast.Call(func=ast.Name(id='Path', ctx=ast.Load()),
                             args=[ast.Constant(str(tmp_path))], keywords=[])
    ast.fix_missing_locations(tree)
    (tmp_path/'data').mkdir(exist_ok=True)
    monkeypatch.setattr(os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(subprocess, 'check_output', docker)
    exec(compile(tree, str(source), 'exec'), {'__name__': '__main__'})


def test_includes_only_scoped_publisher_and_existing_core(tmp_path, monkeypatch):
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        assert kwargs == {'text': True, 'timeout': 10}
        if args[1] == 'ps':
            if 'label=com.docker.compose.project=lighthouse-hub' in args:
                return 'aaaaaaaaaaaa\n'
            assert args == ['/usr/bin/docker', 'ps', '-a', '--filter',
                'label=com.docker.compose.project=lighthouse-publication', '--filter',
                'label=com.docker.compose.service=replay-publication-worker',
                '--format', '{{.ID}}']
            return 'bbbbbbbbbbbb\naaaaaaaaaaaa\n'
        assert args[1:3] == ['inspect', '--format']
        assert 'Env' not in args[3] and 'Mounts' not in args[3]
        name = 'api' if args[-1] == 'aaaaaaaaaaaa' else 'replay-publication-worker'
        return f'/{name}|running|0|0|healthy'
    execute(tmp_path, monkeypatch, docker)
    snapshot = json.loads((tmp_path/'data/host-status.json').read_text())
    assert [x['name'] for x in snapshot['containers']] == ['api', 'replay-publication-worker']
    assert len([x for x in calls if x[1] == 'inspect']) == 2
    assert all(set(x) == {'name','state','exit_code','restart_count','health'}
               for x in snapshot['containers'])


@pytest.mark.parametrize('failure', ['invalid_id', 'docker_failure'])
def test_failed_publisher_read_preserves_previous_timestamp(tmp_path, monkeypatch, failure):
    (tmp_path/'data').mkdir()
    previous = b'{"checked_at":123,"containers":[]}'
    target = tmp_path/'data/host-status.json'
    target.write_bytes(previous)
    def docker(args, **kwargs):
        assert args[1] == 'ps', 'Invalid identity must never reach inspect'
        if 'label=com.docker.compose.project=lighthouse-hub' in args:
            return 'aaaaaaaaaaaa\n'
        if failure == 'docker_failure':
            raise subprocess.CalledProcessError(1, args)
        return 'not-a-container-id\n'
    with pytest.raises((AssertionError, subprocess.CalledProcessError)):
        execute(tmp_path, monkeypatch, docker)
    assert target.read_bytes() == previous
