#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
# Caller MUST already have validated a separate lighthouse-ops key+sudo SSH connection.
test "${LIGHTHOUSE_OPS_KEY_LOGIN_VERIFIED:-}" = yes
test -s /home/lighthouse-ops/.ssh/authorized_keys
install -d -m 755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/00-lighthouse.conf <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
EOF
sshd -t
systemctl reload ssh
# Preserve current and future SSH access before enabling the host firewall.
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp comment 'Lighthouse SSH maintenance'
ufw --force enable
ufw status verbose
sshd -T | grep -E '^(pubkeyauthentication|passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
