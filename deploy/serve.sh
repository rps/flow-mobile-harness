#!/usr/bin/env bash
# Start or stop the harness web UI on the VM (127.0.0.1 only).
#   deploy/serve.sh start|stop|status|token|run|install-service
# The UI token lives in ~/.config/labs/ui_token (0600), generated once on the VM
# so it survives restarts. Reach the UI only through an ssh tunnel (IAP for the
# owner, the reviewer account for testers; see RUNBOOK.md and REVIEWER.md).
# `run` is the foreground form used by the systemd user service that
# `install-service` sets up so the UI comes back on its own after a reboot.
# Real runs need the API key: ~/.config/labs/env is loaded when present.
set -euo pipefail
LABS_DIR="${LABS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# gcloud --command shells are non-interactive and skip ~/.bashrc, so load the VM environment here.
[ -f "$HOME/labs-env.sh" ] && . "$HOME/labs-env.sh"
if [ -f "$HOME/.config/labs/env" ]; then set -a; . "$HOME/.config/labs/env"; set +a; fi
PORT="${HARNESS_UI_PORT:-8765}"
TOKEN_FILE="$HOME/.config/labs/ui_token"
PIDFILE="$HOME/.config/labs/server.pid"
LOG="$HOME/labs-server.log"

token() {
  if [ ! -s "$TOKEN_FILE" ]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"
    (umask 077; "$LABS_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))' >"$TOKEN_FILE")
  fi
  cat "$TOKEN_FILE"
}
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
SERVER_ARGS=(--port "$PORT" --policy-default "${HARNESS_POLICY_DEFAULT:-approve}")
[ "${HARNESS_FAKE_DEFAULT:-0}" = 1 ] && SERVER_ARGS+=(--fake-default)

prepare() {
    [ -x "$LABS_DIR/.venv/bin/python" ] || { echo "no venv at $LABS_DIR/.venv; run bootstrap_vm.sh" >&2; exit 1; }
    # A real (non-fake) job inherits these; without them the manager falls back to the Mac's AVD
    # name, the arm64 image and ~/Library/Android/sdk. Refuse to start rather than fail later.
    for v in ANDROID_HOME LABS_AVD_NAME LABS_AVD_PORT LABS_SYSTEM_IMAGE; do
      [ -n "${!v:-}" ] || { echo "$v is not set: re-run deploy/bootstrap_vm.sh (rewrites ~/labs-env.sh) or export it; refusing to start" >&2; exit 1; }
    done
    export ANDROID_HOME LABS_AVD_NAME LABS_AVD_PORT LABS_SYSTEM_IMAGE
    echo "device env: ANDROID_HOME=$ANDROID_HOME LABS_AVD_NAME=$LABS_AVD_NAME LABS_AVD_PORT=$LABS_AVD_PORT LABS_SYSTEM_IMAGE=$LABS_SYSTEM_IMAGE"
    [ -n "${ANTHROPIC_API_KEY:-}" ] || echo "warning: ANTHROPIC_API_KEY not set (no ~/.config/labs/env); only fake runs will work" >&2
    if ss -Htln "sport = :$PORT" | grep -q .; then
      echo "port $PORT is already in use by another process (stale server?): $(ss -Hltnp "sport = :$PORT" | grep -o 'users:.*')" >&2; exit 1
    fi
    cd "$LABS_DIR"
    tok="$(token)"   # resolve before starting so the state dir exists
    export HARNESS_UI_TOKEN="$tok" HARNESS_RUNS_DIR="${HARNESS_RUNS_DIR:-$LABS_DIR/runs}"
}

case "${1:-}" in
  run)
    prepare
    exec "$LABS_DIR/.venv/bin/python" -m harness.server "${SERVER_ARGS[@]}" "${@:2}" ;;
  start)
    if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    prepare
    nohup "$LABS_DIR/.venv/bin/python" -m harness.server "${SERVER_ARGS[@]}" "${@:2}" >"$LOG" 2>&1 </dev/null &
    pid=$!; echo "$pid" >"$PIDFILE"
    owns_port() { ss -Hltnp "sport = :$PORT" | grep -q "pid=$pid,"; }
    for _ in $(seq 1 30); do
      owns_port && break
      kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; echo "server exited; see $LOG" >&2; tail -5 "$LOG" >&2; exit 1; }
      sleep 0.5
    done
    if ! owns_port; then
      echo "server (pid $pid) did not start listening on 127.0.0.1:$PORT within 15 s; see $LOG" >&2; tail -5 "$LOG" >&2; exit 1
    fi
    echo "listening: $(ss -Htln "sport = :$PORT" | awk '{print $4}')  (pid $pid, log $LOG)"
    echo "token: deploy/serve.sh token" ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; sleep 1; echo "stopped"; else echo "not running"; fi
    rm -f "$PIDFILE" ;;
  status)
    if running; then echo "running pid $(cat "$PIDFILE"): $(ss -Htln "sport = :$PORT" | awk '{print $4}')"; else echo "not running"; exit 1; fi ;;
  token) token ;;
  install-service)
    # systemd user unit; linger lets it start at boot without a login session.
    unit_dir="$HOME/.config/systemd/user"; mkdir -p "$unit_dir"
    cat >"$unit_dir/labs-server.service" <<UNIT
[Unit]
Description=harness web UI (127.0.0.1:$PORT)
After=network-online.target

[Service]
ExecStart=$LABS_DIR/deploy/serve.sh run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    sudo loginctl enable-linger "$USER"
    systemctl --user daemon-reload
    systemctl --user enable --now labs-server.service
    systemctl --user --no-pager status labs-server.service | head -5
    echo "installed; start/stop/status (pidfile) do not apply to it, use: systemctl --user {status,restart,stop} labs-server" ;;
  *) echo "usage: $0 start|stop|status|token|run|install-service" >&2; exit 2 ;;
esac
