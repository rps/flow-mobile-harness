#!/usr/bin/env bash
# Install or update the idle auto-stop on the VM. Run ON the VM:
#   sudo deploy/autostop/install.sh [IDLE_MINUTES] [RUNS_DIR]
# Defaults: 30 min; RUNS_DIR = $HARNESS_RUNS_DIR, else $LABS_DIR/runs, else <this checkout>/runs (same
# resolution as serve.sh; pass them through sudo with `sudo LABS_DIR=... install.sh`).
# An existing /etc/default/labs-autostop is kept unless arguments are given.
# Uninstall: sudo systemctl disable --now labs-autostop.timer
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
idle="${1:-30}"
home="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
repo="$(cd "$HERE/../.." && pwd)"   # the checkout this installer lives in
runs_dir="${2:-${HARNESS_RUNS_DIR:-${LABS_DIR:-$repo}/runs}}"

install -m 0755 "$HERE/labs-autostop.sh" /usr/local/sbin/labs-autostop.sh
install -m 0644 "$HERE/labs-autostop.service" "$HERE/labs-autostop.timer" /etc/systemd/system/
if [ ! -f /etc/default/labs-autostop ] || [ -n "${1:-}" ] || [ -n "${2:-}" ]; then
  printf 'IDLE_MINUTES=%s\nRUNS_DIR=%s\n' "$idle" "$runs_dir" >/etc/default/labs-autostop
fi
systemctl daemon-reload
systemctl enable --now labs-autostop.timer >/dev/null
echo "installed; config:"; sed 's/^/  /' /etc/default/labs-autostop
systemctl list-timers labs-autostop.timer --no-pager | head -2
