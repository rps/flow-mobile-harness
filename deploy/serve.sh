#!/usr/bin/env bash
# Start or stop the harness web UI on the VM (fake mode, 127.0.0.1 only).
#   deploy/serve.sh start|stop|status|token
# The UI token lives in ~/.config/labs/ui_token (0600), generated once on the VM
# so it survives restarts. Reach the UI only through an IAP tunnel; see RUNBOOK.md.
set -euo pipefail
LABS_DIR="${LABS_DIR:-$HOME/labs}"
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

case "${1:-}" in
  start)
    if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    [ -x "$LABS_DIR/.venv/bin/python" ] || { echo "no venv at $LABS_DIR/.venv; run bootstrap_vm.sh" >&2; exit 1; }
    if ss -Htln "sport = :$PORT" | grep -q .; then
      echo "port $PORT is already in use by another process (stale server?): $(ss -Hltnp "sport = :$PORT" | grep -o 'users:.*')" >&2; exit 1
    fi
    cd "$LABS_DIR"
    tok="$(token)"   # resolve before backgrounding so the state dir exists
    # HARNESS_UI_TOKEN is read by harness.server; nothing else from the Mac is needed in fake mode.
    HARNESS_UI_TOKEN="$tok" HARNESS_RUNS_DIR="${HARNESS_RUNS_DIR:-$LABS_DIR/runs}" \
      nohup "$LABS_DIR/.venv/bin/python" -m harness.server --port "$PORT" --fake-default "${@:2}" >"$LOG" 2>&1 </dev/null &
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
  *) echo "usage: $0 start|stop|status|token" >&2; exit 2 ;;
esac
