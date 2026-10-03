#!/usr/bin/env bash
# Push the repo to the VM as a git bundle of one ref (default main) over IAP scp
# and check it out there. Never rsyncs the working tree, so .env, .venv, apks/
# and runs/ can never leave this machine by accident.
#
#   deploy/push_source.sh [REF]        REF: local branch, tag or commit (a commit is bundled via a transient ref)
#   LABS_REMOTE_DIR=labs               checkout directory under $HOME on the VM
#
# The VM's provisioned harness/emulator/baseline.json and stock_apps.txt (written by
# `manager provision` on the VM, tracked in git with the Mac's values) are backed up to
# ~/.config/labs/baseline/ before the checkout and copied back afterwards, so a change to
# those files on main neither blocks the checkout nor replaces the VM's baseline record.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/gcloud.sh
source "$HERE/lib/gcloud.sh"

REF="${1:-main}"
REPO="${LABS_REPO:-$(cd "$HERE/.." && pwd)}"
REMOTE_DIR="${LABS_REMOTE_DIR:-labs}"

TMP="$(mktemp -d)"
TMP_REF=""
cleanup() { rm -rf "$TMP"; [ -n "$TMP_REF" ] && git -C "$REPO" update-ref -d "$TMP_REF"; }
trap cleanup EXIT

if git -C "$REPO" show-ref --verify --quiet "refs/heads/$REF" || git -C "$REPO" show-ref --verify --quiet "refs/tags/$REF"; then
  SHA="$(git -C "$REPO" rev-parse "${REF}^{commit}")"   # peels annotated tags
  BUNDLE_REF="$REF"
elif SHA="$(git -C "$REPO" rev-parse --verify --quiet "${REF}^{commit}")"; then
  # git bundle needs a ref name: point a transient one (deleted on exit) at the commit.
  TMP_REF="refs/deploy/tmp-$SHA"
  git -C "$REPO" update-ref "$TMP_REF" "$SHA"
  BUNDLE_REF="$TMP_REF"
else
  echo "error: '$REF' is not a branch, tag or commit in $REPO" >&2
  exit 2
fi
BUNDLE="$TMP/labs.bundle"

echo "[push] bundling $REF ($SHA) from $REPO"
git -C "$REPO" bundle create "$BUNDLE" "$BUNDLE_REF" 2>&1 | tail -1
ls -lh "$BUNDLE" | awk '{print "[push] bundle size " $5}'

echo "[push] uploading over IAP"
vm_scp "$BUNDLE" "/tmp/labs.bundle"

echo "[push] checking out on the VM into ~/$REMOTE_DIR"
vm_ssh "set -euo pipefail
  mkdir -p ~/$REMOTE_DIR && cd ~/$REMOTE_DIR
  [ -d .git ] || git init -q
  git bundle verify /tmp/labs.bundle >/dev/null
  git fetch -q /tmp/labs.bundle '$BUNDLE_REF'
  keep='harness/emulator/baseline.json harness/emulator/stock_apps.txt'
  bak=~/.config/labs/baseline
  kept=0
  if [ -d .git ] && git rev-parse -q --verify HEAD >/dev/null && ! git diff --quiet -- \$keep 2>/dev/null; then
    mkdir -p \$bak && cp -p \$keep \$bak/ && git checkout -q -- \$keep && kept=1 && echo '[vm] VM baseline files backed up to ~/.config/labs/baseline'
  fi
  git checkout -q -B deploy FETCH_HEAD
  if [ \$kept = 1 ]; then cp -p \$bak/baseline.json \$bak/stock_apps.txt harness/emulator/ && echo '[vm] VM baseline files restored over the checkout'; fi
  rm -f /tmp/labs.bundle
  echo \"[vm] HEAD: \$(git log --oneline -1)\"
  [ \"\$(git rev-parse HEAD)\" = '$SHA' ] || { echo '[vm] HEAD does not match the bundled ref' >&2; exit 1; }
  if [ -x .venv/bin/pip ]; then .venv/bin/pip install -q -e . && echo '[vm] venv deps synced'; else echo '[vm] no .venv yet: run deploy/bootstrap_vm.sh on the VM'; fi"
