#!/usr/bin/env bash
# Shared helpers for the Mac-side deploy scripts. Source, do not execute.
# All access goes through IAP (gcloud ... --tunnel-through-iap); nothing is ever
# opened to the public internet.

LABS_VM="${LABS_VM:-<VM>}"
LABS_ZONE="${LABS_ZONE:-us-east1-b}"
LABS_PROJECT="${LABS_PROJECT:-<PROJECT>}"
# IAP returns 4047/4003 (ssh exit 255) for a minute or so after the VM starts.
LABS_SSH_RETRIES="${LABS_SSH_RETRIES:-8}"

gcloud_p() { gcloud --project="$LABS_PROJECT" "$@"; }

vm_status() { gcloud_p compute instances describe "$LABS_VM" --zone="$LABS_ZONE" --format='value(status)'; }

vm_start() {
  local st; st="$(vm_status)"
  if [ "$st" = "RUNNING" ]; then echo "[vm] already RUNNING"; return 0; fi
  echo "[vm] start at $(date -u +%FT%TZ) (status was $st)"
  gcloud_p compute instances start "$LABS_VM" --zone="$LABS_ZONE"
}

vm_stop() {
  echo "[vm] stop at $(date -u +%FT%TZ)"
  gcloud_p compute instances stop "$LABS_VM" --zone="$LABS_ZONE"
  echo "[vm] status: $(vm_status)"
}

# vm_ssh 'remote command' — retries only on ssh exit 255 (tunnel/IAP failures),
# never on a non-zero exit of the remote command itself.
vm_ssh() {
  local i rc
  for ((i = 1; i <= LABS_SSH_RETRIES; i++)); do
    gcloud_p compute ssh "$LABS_VM" --zone="$LABS_ZONE" --tunnel-through-iap --quiet --command="$1" && return 0
    rc=$?
    [ "$rc" -ne 255 ] && return "$rc"
    echo "[vm] ssh attempt $i failed (rc=255, IAP not ready?); retrying in 10s" >&2
    sleep 10
  done
  return 255
}

# vm_scp LOCAL REMOTE_PATH — same retry policy as vm_ssh.
vm_scp() {
  local i rc
  for ((i = 1; i <= LABS_SSH_RETRIES; i++)); do
    gcloud_p compute scp "$1" "$LABS_VM:$2" --zone="$LABS_ZONE" --tunnel-through-iap --quiet && return 0
    rc=$?
    [ "$rc" -ne 255 ] && return "$rc"
    echo "[vm] scp attempt $i failed (rc=255); retrying in 10s" >&2
    sleep 10
  done
  return 255
}
