#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get install -y ca-certificates curl gnupg git jq ufw python3 python3-venv ffmpeg unattended-upgrades
if dpkg-query -W docker.io podman-docker 2>/dev/null | grep -q '^[^ ]*[[:space:]][0-9]'; then
  echo 'Conflicting engine found; stop for inspection without removing anything' >&2
  exit 1
fi
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
install -d -m 755 /etc/docker
python3 - <<'PY'
from pathlib import Path
import json
p=Path('/etc/docker/daemon.json')
cfg=json.loads(p.read_text()) if p.exists() else {}
if p.exists() and not p.with_suffix('.json.pre-lighthouse').exists():
    p.with_suffix('.json.pre-lighthouse').write_text(p.read_text())
cfg['log-driver']='local'
cfg['log-opts']={'max-size':'10m','max-file':'3'}
p.write_text(json.dumps(cfg,indent=2)+'\n')
PY
systemctl enable --now docker
systemctl restart docker
docker version --format '{{.Server.Version}}'
docker compose version
echo 'ENGINE_READY'
