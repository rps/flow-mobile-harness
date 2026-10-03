#!/usr/bin/env bash
# Create the tunnel-only `reviewer` account on the VM. Run as root:
#   sudo deploy/reviewer/setup_reviewer.sh /path/to/reviewer_key.pub [PORT]
# The account has no shell access: key-only login, and the only thing sshd lets
# it do is forward 127.0.0.1:PORT (the harness UI). Idempotent; re-run with a new
# public key to rotate it.
set -euo pipefail
PUB="${1:?public key file}"; PORT="${2:-8765}"; USER_NAME=reviewer
[ -s "$PUB" ] || { echo "no such key file: $PUB" >&2; exit 1; }
grep -q '^ssh-' "$PUB" || { echo "$PUB does not look like a public key" >&2; exit 1; }

id -u "$USER_NAME" >/dev/null 2>&1 || useradd --create-home --shell /bin/sh "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null

# Shown if the reviewer omits -N: keep the session alive instead of exiting.
cat >/usr/local/bin/labs-tunnel-hold <<'HOLD'
#!/bin/sh
echo "tunnel open: keep this window open and use http://127.0.0.1:8765/ in your browser (Ctrl-C to disconnect)"
exec sleep infinity
HOLD
chmod 755 /usr/local/bin/labs-tunnel-hold

home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$home/.ssh"
printf 'restrict,port-forwarding,permitopen="127.0.0.1:%s" %s\n' "$PORT" "$(grep '^ssh-' "$PUB" | head -1)" \
  >"$home/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"

cat >/etc/ssh/sshd_config.d/60-labs-reviewer.conf <<CONF
# labs reviewer: tunnel-only account (deploy/reviewer/setup_reviewer.sh)
ClientAliveInterval 60
ClientAliveCountMax 5
Match User $USER_NAME
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile .ssh/authorized_keys
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    PermitOpen 127.0.0.1:$PORT
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PermitTunnel no
    ForceCommand /usr/local/bin/labs-tunnel-hold
CONF
sshd -t
systemctl reload ssh
echo "reviewer account ready: ssh -i reviewer_key -N -L $PORT:127.0.0.1:$PORT reviewer@<VM_IP>"
