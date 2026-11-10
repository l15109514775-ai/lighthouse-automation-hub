"""Read-only comparison of a private rendered Compose release and live Docker.

Does not render environment values, execute application handlers or change Docker.
This verifies selected runtime fields, not application/database correctness.
"""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def size(value):
    if type(value) is int:
        return value
    match = re.fullmatch(r'(\d+)([bkmg]?)', str(value).lower())
    if not match:
        raise ValueError('UNSUPPORTED_MEMORY_FORMAT')
    return int(match[1]) * {'': 1, 'b': 1, 'k': 1024, 'm': 1024**2, 'g': 1024**3}[match[2]]


def environment(value):
    if isinstance(value, dict):
        if any(v is None for v in value.values()):
            raise ValueError('UNRESOLVED_ENVIRONMENT')
        return {k: str(v) for k, v in value.items()}
    if not isinstance(value, list) or any(not isinstance(x, str) or '=' not in x for x in value):
        raise ValueError('UNRESOLVED_ENVIRONMENT')
    parsed = dict(x.split('=', 1) for x in value)
    if len(parsed) != len(value):
        raise ValueError('DUPLICATE_ENVIRONMENT')
    return parsed


def security_options(options, profiles):
    normalized = []
    for value in options:
        value = value.replace('=', ':', 1)
        if value.startswith('seccomp:'):
            body = value[len('seccomp:'):]
            if body.startswith('/'):
                if body not in profiles:
                    raise ValueError('SECCOMP_PROFILE_NOT_LOADED')
                body = profiles[body]
            elif body.startswith('{'):
                body = json.loads(body)
            if isinstance(body, dict):
                value = 'seccomp:' + json.dumps(body, sort_keys=True, separators=(',', ':'))
        normalized.append(value)
    return sorted(normalized)


def compare(service, actual, image, security_profiles=None):
    cfg, host = actual['Config'], actual['HostConfig']
    checks = {'image': actual['Image'] == image, 'running': actual['State']['Running']}
    fields = {'user': ('User', str), 'working_dir': ('WorkingDir', str),
              'command': ('Cmd', list), 'entrypoint': ('Entrypoint', list),
              'stdin_open': ('OpenStdin', bool), 'tty': ('Tty', bool)}
    for key, (target, kind) in fields.items():
        if key in service and service[key] is not None:
            if key in ('command', 'entrypoint') and not isinstance(service[key], list):
                raise ValueError('UNRENDERED_COMMAND')
            checks[key] = cfg.get(target) == kind(service[key])
    for key, target, convert in [('read_only', 'ReadonlyRootfs', bool),
                                 ('mem_limit', 'Memory', size), ('memswap_limit', 'MemorySwap', size),
                                 ('cpus', 'NanoCpus', lambda x: int(Decimal(str(x)) * 1_000_000_000)),
                                 ('pids_limit', 'PidsLimit', int), ('init', 'Init', bool)]:
        if key in service:
            checks[key] = host.get(target) == convert(service[key])
    for key, target in [('cap_drop', 'CapDrop'), ('cap_add', 'CapAdd'), ('group_add', 'GroupAdd')]:
        if key in service:
            checks[key] = sorted(map(str, service[key])) == sorted(map(str, host.get(target) or []))
    if 'security_opt' in service:
        profiles = security_profiles or {}
        checks['security_opt'] = security_options(service['security_opt'], profiles) == security_options(host.get('SecurityOpt') or [], profiles)
    if 'restart' in service:
        parts = service['restart'].split(':')
        checks['restart'] = host['RestartPolicy']['Name'] == parts[0]
        checks['restart_count_limit'] = host['RestartPolicy']['MaximumRetryCount'] == (int(parts[1]) if len(parts) == 2 else 0)
    if 'logging' in service:
        log = service['logging']
        checks['logging'] = host['LogConfig'] == {'Type': log['driver'], 'Config': {k: str(v) for k, v in log.get('options', {}).items()}}
    expected_env = environment(service.get('environment', []))
    actual_env = environment(cfg.get('Env') or [])
    checks['explicit_environment'] = all(actual_env.get(k) == v for k, v in expected_env.items())
    expected_mounts = service.get('volumes', [])
    if any(not isinstance(m, dict) or m.get('type') != 'bind' for m in expected_mounts):
        raise ValueError('UNSUPPORTED_MOUNT_DECLARATION')
    target_map = {m['Destination']: m for m in actual['Mounts']}
    checks['mount_targets'] = {p for p, m in target_map.items() if m['Type'] != 'tmpfs'} == {m['target'] for m in expected_mounts}
    checks['bind_mounts'] = all(
        m['target'] in target_map and target_map[m['target']]['Type'] == 'bind'
        and target_map[m['target']]['Source'] == m['source']
        and target_map[m['target']]['RW'] == (not m.get('read_only', False))
        for m in expected_mounts)
    return {'checks': checks, 'matches': all(checks.values()), 'image': actual['Image'],
            'container_id': actual['Id'], 'restart_count': actual['RestartCount'],
            'health': actual['State'].get('Health', {}).get('Status'),
            'explicit_environment_count': len(expected_env)}


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('DOCKER_READ_FAILED')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, action='append', required=True)
    parser.add_argument('--service', action='append', required=True)
    parser.add_argument('--project-name', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not args.output.is_absolute() or args.output.parent.resolve() != args.output.parent:
        raise ValueError('OUTPUT_REQUIRES_NEW_ABSOLUTE_PATH')
    services = {}
    for path in args.manifest:
        rendered = json.loads(docker('compose', '-p', args.project_name, '-f', str(path),
                                     '--project-directory', str(path.parent), '--profile', '*',
                                     'config', '--format', 'json'))
        for name, value in rendered['services'].items():
            for secret in value.get('secrets', []):
                definition = rendered['secrets'][secret['source']]
                if definition.get('external') or not definition.get('file'):
                    raise ValueError('UNSUPPORTED_SECRET_DECLARATION')
                target = secret.get('target', secret['source'])
                target = target if target.startswith('/') else '/run/secrets/' + target
                value.setdefault('volumes', []).append({'type': 'bind', 'source': definition['file'],
                                                       'target': target, 'read_only': True})
            if name in services and services[name] != value:
                raise ValueError('CONFLICTING_SERVICE_MANIFESTS')
            services[name] = value
    results = {}
    for name in args.service:
        service = services[name]
        if service.get('container_name'):
            name_or_id = service['container_name']
        else:
            ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + args.project_name,
                         '--filter', 'label=com.docker.compose.service=' + name).decode().splitlines()
            if len(ids) != 1:
                raise ValueError('SERVICE_CONTAINER_NOT_UNIQUE')
            name_or_id = ids[0]
        actual = json.loads(docker('inspect', name_or_id))[0]
        image = json.loads(docker('image', 'inspect', service['image']))[0]['Id']
        profiles = {}
        for option in service.get('security_opt', []):
            normalized = option.replace('=', ':', 1)
            if normalized.startswith('seccomp:/'):
                profile = Path(normalized[len('seccomp:'):])
                if (not profile.is_file() or profile.is_symlink() or any(p.is_symlink() for p in profile.parents)
                        or profile.stat().st_size > 1_000_000):
                    raise ValueError('UNSAFE_SECCOMP_PROFILE')
                profiles[str(profile)] = json.loads(profile.read_text())
        result = compare(service, actual, image, profiles)
        code = []
        for mount in actual['Mounts']:
            target = mount['Destination']
            # Never read data, configuration, profiles, credentials or arbitrary mounts.
            if not re.fullmatch(r'/opt/lighthouse-hub/(services|adapters|scheduler)/[A-Za-z0-9_/.-]+\.(py|sql|mjs)', target):
                continue
            source = Path(mount['Source'])
            if mount['Type'] != 'bind' or mount['RW'] or source.is_symlink() or any(p.is_symlink() for p in source.parents):
                raise ValueError('UNSAFE_CODE_MOUNT')
            if not source.is_file() or source.stat().st_size > 2_000_000:
                raise ValueError('CODE_MOUNT_SIZE')
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            inside = docker('exec', actual['Id'], 'sha256sum', target).decode().split()[0]
            code.append({'target': target, 'source': str(source), 'sha256': sha,
                         'container_matches_host': inside == sha})
        result['code_overlays'] = code
        result['matches'] = result['matches'] and all(x['container_matches_host'] for x in code)
        results[name] = result
    report = {'version': 'compose-runtime-audit-v1', 'at': time.time(), 'services': results,
              'matches': all(x['matches'] for x in results.values()), 'provider_calls': 0,
              'limits': ['Selected declared fields only; no database or provider validation',
                         'Port/network/healthcheck/tmpfs contents not compared',
                         'Inherited image environment not compared; explicit environment compared without disclosure',
                         'No directory, secret, configuration content or media copied']}
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(report, f, indent=2); f.write('\n')
    print(json.dumps({'matches': report['matches'], 'service_count': len(results),
                      'failed_checks': {k: [c for c, ok in r['checks'].items() if not ok] for k, r in results.items() if not r['matches']}}))
    return 0 if report['matches'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
