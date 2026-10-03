#!/usr/bin/env bash
# Mac side: prepare everything an outside reviewer needs to use the VM's web UI
# without gcloud, a Google account, or any install (plain ssh tunnel).
#   deploy/reviewer/make_handoff.sh
# Writes deploy/private/ (gitignored): reviewer_key, reviewer_key.pub and
# REVIEWER.md with the VM's static IP and the UI URL filled in. Send the
# reviewer that directory's reviewer_key and REVIEWER.md.
# On the VM: creates/rotates the tunnel-only account, installs the systemd user
# service for the UI (if not yet), and disables the idle auto-stop so the VM
# stays up for an unattended review window.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/../lib/gcloud.sh"
PRIV="$here/../private"; mkdir -p "$PRIV"; chmod 700 "$PRIV"
PORT="${HARNESS_UI_PORT:-8765}"

[ -f "$PRIV/reviewer_key" ] || ssh-keygen -t ed25519 -N '' -C labs-reviewer -f "$PRIV/reviewer_key" >/dev/null
vm_start >/dev/null
ip="$(gcloud_p compute instances describe "$LABS_VM" --zone="$LABS_ZONE" \
      --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
[ -n "$ip" ] || { echo "VM has no external IP" >&2; exit 1; }

vm_scp "$PRIV/reviewer_key.pub" /tmp/reviewer_key.pub
vm_ssh "sudo ~/labs/deploy/reviewer/setup_reviewer.sh /tmp/reviewer_key.pub $PORT && rm -f /tmp/reviewer_key.pub"
vm_ssh 'systemctl --user is-enabled labs-server.service >/dev/null 2>&1 || ~/labs/deploy/serve.sh install-service'
vm_ssh 'sudo systemctl disable --now labs-autostop.timer 2>/dev/null; echo "autostop: $(systemctl is-enabled labs-autostop.timer 2>&1 | tail -1)"'
token="$(vm_ssh '~/labs/deploy/serve.sh token' 2>/dev/null | tail -1)"
[ -n "$token" ] || { echo "could not read the UI token" >&2; exit 1; }

sed -e "s|<VM_IP>|$ip|g" -e "s|<TOKEN>|$token|g" -e "s|<PORT>|$PORT|g" "$here/../REVIEWER.md" >"$PRIV/REVIEWER.md"
chmod 600 "$PRIV"/*
echo "handoff ready in $PRIV: send reviewer_key and REVIEWER.md"
echo "self-check: ssh -i $PRIV/reviewer_key -o StrictHostKeyChecking=accept-new -N -L $PORT:127.0.0.1:$PORT reviewer@$ip"
