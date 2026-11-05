"""Run the approved private OpenAI tunnel, separate from all publishing services.

The dedicated runtime key permits only Tunnels Read/Use. No public port is
published. The child exposes a fixed five-tool snapshot reader, never shell.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time

IMAGE = 'sha256:5c5391b8bf313525f6fc7c43db40ad6832b6c6b431e58a5aeb0bcf04bdc4e981'
TUNNEL = 'tunnel_6ae9869af484819183e1081ba1103cb7'
NAME = 'lighthouse-observer-tunnel-v1'
BINARY = Path('/opt/lighthouse-observer-tunnel-v1/tunnel-client')
BINARY_SHA = '56d0120886ce91bfe31e7a5600f6a6594199a02e9b3eac671d5da0d7480c97a3'
READER = Path('/opt/lighthouse-observer-v1/observer_mcp_v1.py')
READER_SHA = '4c301d2afdecf6a557ac8cca9f5879d4d57a83104bcc362eb0885d611386f580'
SNAPSHOT = Path('/var/lib/lighthouse-observer-v1/snapshot')
SECRETS = Path('/etc/lighthouse-observer-v1')
KEYFILE = SECRETS / 'control-plane.key'
CHILD = ('/usr/bin/env -i PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 '
         'PYTHONDONTWRITEBYTECODE=1 HUB_OBSERVER_SNAPSHOT=/snapshot/status.json '
         '/opt/venv/bin/python -u -I -B /observer/observer_mcp_v1.py')


def need(value, code):
    if not value:
        raise ValueError(code)


def run(args, *, timeout=40, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    need(not check or p.returncode == 0, 'TUNNEL_OPERATOR_COMMAND_FAILED')
    return p


def safe_file(path, *, digest=None):
    need(path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)), 'UNSAFE_TUNNEL_PATH')
    info = path.stat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, 'ROOT_CONTROLLED_FILE_REQUIRED')
    if digest:
        need(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'TUNNEL_SOURCE_HASH_MISMATCH')


def core():
    raw = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=lighthouse-hub']).stdout.split()
    values = json.loads(run(['docker', 'inspect', *raw]).stdout)
    need(len(values) == 5 and all(v['State']['Running'] for v in values), 'CORE_FIVE_SERVICES_REQUIRED')
    return sorted((v['Id'], v['Image'], v['State']['StartedAt']) for v in values)


def common():
    mounts = [(BINARY, '/client/tunnel-client'), (READER, '/observer/observer_mcp_v1.py'),
              (SNAPSHOT, '/snapshot'), (KEYFILE, '/run/secrets/control-plane.key')]
    args = ['--read-only', '--no-healthcheck', '--user', '10001:10001', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--network', 'bridge',
            '--memory', '268435456', '--cpus', '0.25', '--pids-limit', '64',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001']
    for source, target in mounts:
        args += ['--mount', f'type=bind,src={source},dst={target},readonly']
    return args + ['--entrypoint', '/client/tunnel-client', IMAGE]


def client_flags():
    return ['--control-plane.tunnel-id', TUNNEL,
            '--control-plane.api-key', 'file:/run/secrets/control-plane.key',
            '--mcp.command', CHILD, '--mcp.stdio-failure-policy', 'exit',
            '--health.listen-addr', '127.0.0.1:8091',
            '--allow-remote-ui=false', '--open-web-ui=false',
            '--log.level', 'info', '--log.format', 'json']


def verify_container(v, image_env=None):
    if image_env is None:
        image_env = json.loads(run(['docker', 'image', 'inspect', IMAGE]).stdout)[0]['Config']['Env']
    h = v['HostConfig']
    expected = sorted([(str(BINARY), '/client/tunnel-client', False),
                       (str(READER), '/observer/observer_mcp_v1.py', False),
                       (str(SNAPSHOT), '/snapshot', False),
                       (str(KEYFILE), '/run/secrets/control-plane.key', False)])
    need(v['Image'] == IMAGE and v['Config']['User'] == '10001:10001' and
         h['ReadonlyRootfs'] and h['CapDrop'] == ['ALL'] and not h.get('CapAdd') and
         not h.get('Privileged') and not h.get('PortBindings') and not h.get('PublishAllPorts') and
         not h.get('Devices') and not h.get('DeviceRequests') and not h.get('VolumesFrom') and not h.get('Binds') and
         h.get('PidMode', '') == '' and h['NetworkMode'] == 'bridge' and
         h['SecurityOpt'] == ['no-new-privileges'] and
         h['Memory'] == 268435456 and h['NanoCpus'] == 250000000 and h['PidsLimit'] == 64 and
         h['LogConfig'] == {'Type': 'local', 'Config': {'max-size': '2m', 'max-file': '3'}} and
         h.get('Tmpfs') == {'/tmp': 'rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001'} and
         h.get('IpcMode') in ('private', '') and h.get('UTSMode', '') == '' and
         h['RestartPolicy']['Name'] == 'unless-stopped' and
         v['Config']['Labels'].get('io.lighthouse.observer.role') == 'tunnel-v1' and
         v['Config'].get('Healthcheck') == {'Test': ['NONE']} and
         sorted(v['Config']['Env']) == sorted(image_env) and
         set(v.get('NetworkSettings', {}).get('Networks', {})) <= {'bridge'} and
         v['Config']['Entrypoint'] == ['/client/tunnel-client'] and
         v['Config']['Cmd'] == ['run', *client_flags()] and
         sorted((m['Source'], m['Destination'], m['RW']) for m in v['Mounts'] if m['Type'] == 'bind') == expected and
         all(m['Type'] == 'bind' or m['Type'] == 'tmpfs' and m['Destination'] == '/tmp' for m in v['Mounts']),
         'EXISTING_TUNNEL_SCOPE_CHANGED')
    need(not any(re.search(r'TOKEN|SECRET|PASSWORD|COOKIE|API_KEY|ACCESS_KEY', e.split('=', 1)[0], re.I)
                 for e in v['Config']['Env']), 'SECRET_ENVIRONMENT_REFUSED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    need(os.geteuid() == 0, 'ROOT_OPERATOR_REQUIRED')
    safe_file(BINARY, digest=BINARY_SHA); safe_file(READER, digest=READER_SHA)
    safe_file(SNAPSHOT / 'status.json')
    before = core()
    plan = {'name': NAME, 'image': IMAGE, 'tunnel_id': TUNNEL,
            'binary_sha256': BINARY_SHA, 'reader_sha256': READER_SHA,
            'public_ports': [], 'write_tools': [], 'secret_in_environment': False}
    if not args.apply:
        print(json.dumps({'state': 'plan', **plan})); return
    safe_file(SECRETS / 'control-plane.env')
    raw = (SECRETS / 'control-plane.env').read_bytes()
    need(re.fullmatch(rb'CONTROL_PLANE_API_KEY=sk-[A-Za-z0-9_-]{40,}\n', raw), 'INVALID_PRIVATE_KEY_FORMAT')
    secret = raw.split(b'=', 1)[1].strip()
    if KEYFILE.exists():
        safe_file(KEYFILE); need(KEYFILE.read_bytes().strip() == secret, 'KEYFILE_CONFLICT')
    else:
        with os.fdopen(os.open(KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
            f.write(secret+b'\n'); f.flush(); os.fsync(f.fileno())
        os.chown(KEYFILE, 0, 10001); os.chmod(KEYFILE, 0o640)
    existing = run(['docker', 'inspect', NAME], check=False)
    if existing.returncode:
        doctor = run(['docker', 'run', '--rm', *common(), 'doctor', '--json', '--explain', *client_flags()], timeout=90, check=False)
        # Diagnostic contents may contain credentials on upstream failures: never print raw exceptions.
        safe = (doctor.stdout + doctor.stderr).replace(secret.decode(), '[REDACTED]')
        safe = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', safe)
        report = {'at': time.time(), 'exit_code': doctor.returncode, 'output': safe[-24000:]}
        receipt = Path('/var/lib/lighthouse-observer-v1/tunnel-doctor.json')
        receipt.write_text(json.dumps(report), encoding='utf-8'); receipt.chmod(0o600)
        need(doctor.returncode == 0, 'TUNNEL_DOCTOR_FAILED_SEE_PRIVATE_RECEIPT')
        run(['docker', 'create', '--name', NAME, '--label', 'io.lighthouse.observer.role=tunnel-v1',
             '--restart', 'unless-stopped', '--log-driver', 'local', '--log-opt', 'max-size=2m',
             '--log-opt', 'max-file=3', *common(), 'run', *client_flags()])
    v = json.loads(run(['docker', 'inspect', NAME]).stdout)[0]; verify_container(v)
    if not v['State']['Running']:
        run(['docker', 'start', NAME])
    need(core() == before, 'CORE_CHANGED_DURING_TUNNEL_INSTALL')
    print(json.dumps({'state': 'started_readiness_pending', 'at': time.time(), 'core_unchanged': True, **plan}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z_]+', str(error)) else 'TUNNEL_DEPLOYMENT_FAILED'
        print(json.dumps({'state': 'stopped', 'error_code': code}))
        raise SystemExit(1) from None
