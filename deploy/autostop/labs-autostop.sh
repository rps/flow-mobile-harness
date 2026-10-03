#!/usr/bin/env bash
# labs-autostop: shut the VM down when nobody is using it. Runs as root every
# minute from labs-autostop.timer. "Active" means any of:
#   - an established TCP connection to sshd (an IAP ssh session or tunnel),
#   - a write under RUNS_DIR within the last IDLE_MINUTES (a harness run in
#     progress, from the web UI or the CLI).
# A merely listening harness server does not keep the VM up: a tester who is
# using it has a tunnel open, which is an ssh connection.
#
# The VM has no service account, so `gcloud compute instances stop` cannot run
# here; `shutdown -h now` makes GCE mark the instance TERMINATED, which ends
# compute billing (disk billing continues). Verified in deploy/VM_STATE.md.
set -u
IDLE_MINUTES="${IDLE_MINUTES:-30}"
RUNS_DIR="${RUNS_DIR:-}"
STATE_DIR=/run/labs-autostop
STATE="$STATE_DIR/last_active"
mkdir -p "$STATE_DIR"
# One-boot override for testing (tmpfs, gone after reboot): echo 1 | sudo tee /run/labs-autostop/idle_minutes_override
[ -s "$STATE_DIR/idle_minutes_override" ] && IDLE_MINUTES="$(cat "$STATE_DIR/idle_minutes_override")"

now=$(date +%s)
boot=$(( now - $(cut -d. -f1 /proc/uptime) ))   # first idle window starts at boot
reasons=()

ssh_n=$(ss -Htn state established '( sport = :22 )' | wc -l)
[ "$ssh_n" -gt 0 ] && reasons+=("ssh_connections=$ssh_n")

if [ -n "$RUNS_DIR" ] && [ -d "$RUNS_DIR" ] && [ -n "$(find "$RUNS_DIR" -newermt "-${IDLE_MINUTES} minutes" -print -quit 2>/dev/null)" ]; then
  reasons+=("runs_dir_written_recently")
fi

if [ "${#reasons[@]}" -gt 0 ]; then
  echo "$now" >"$STATE"
  echo "active: ${reasons[*]}"
  exit 0
fi

last=$(cat "$STATE" 2>/dev/null || echo "$boot")
idle=$(( (now - last) / 60 ))
if [ "$idle" -ge "$IDLE_MINUTES" ]; then
  logger -t labs-autostop "idle for ${idle} min (limit ${IDLE_MINUTES}); shutting down"
  echo "idle for ${idle} min; shutting down"
  shutdown -h now "labs-autostop: idle for ${idle} min"
else
  echo "idle ${idle}/${IDLE_MINUTES} min"
fi
