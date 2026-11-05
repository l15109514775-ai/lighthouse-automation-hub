#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
test "$(dpkg --print-architecture)" = amd64

if ! id lighthouse-ops >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --groups sudo lighthouse-ops
fi
install -d -m 700 -o lighthouse-ops -g lighthouse-ops /home/lighthouse-ops/.ssh
touch /home/lighthouse-ops/.ssh/authorized_keys
key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB4r/B6Nc0f/BHaxcSdbySADuUObRC7SbySPmwuHFBYz lighthouse-ops-20261031'
grep -qxF "$key" /home/lighthouse-ops/.ssh/authorized_keys || printf '%s\n' "$key" >> /home/lighthouse-ops/.ssh/authorized_keys
chown lighthouse-ops:lighthouse-ops /home/lighthouse-ops/.ssh/authorized_keys
chmod 600 /home/lighthouse-ops/.ssh/authorized_keys
printf '%s\n' 'lighthouse-ops ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/90-lighthouse-ops
chmod 440 /etc/sudoers.d/90-lighthouse-ops
visudo -cf /etc/sudoers.d/90-lighthouse-ops

if ! id lighthouse-app >/dev/null 2>&1; then
  if getent passwd 10001 >/dev/null; then
    echo 'UID 10001 already used; stop instead of changing an existing account' >&2
    exit 1
  fi
  useradd --system --uid 10001 --user-group --home-dir /opt/lighthouse-hub --shell /usr/sbin/nologin lighthouse-app
fi
install -d -m 755 -o lighthouse-ops -g lighthouse-ops /opt/lighthouse-hub
for folder in services adapters scheduler compositor mcp config docs; do
  install -d -m 755 -o lighthouse-ops -g lighthouse-ops "/opt/lighthouse-hub/$folder"
done
for folder in data media logs backups; do
  install -d -m 750 -o lighthouse-app -g lighthouse-app "/opt/lighthouse-hub/$folder"
done
install -d -m 700 /etc/lighthouse-hub
echo 'ACCOUNTS_READY: verify a NEW lighthouse-ops SSH connection before hardening SSH.'
