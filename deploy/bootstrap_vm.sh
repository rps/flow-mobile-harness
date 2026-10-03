#!/usr/bin/env bash
# Bootstrap the Android/Python toolchain on the GCE VM. Run ON the VM, as the
# OS Login user, after deploy/push_source.sh has placed the repo in $LABS_DIR.
# Idempotent: every step checks before it changes anything.
#
# Choices (see deploy/README.md):
#   Python 3.12 from the deadsnakes PPA (Ubuntu 22.04 ships 3.10 only); pyproject is unchanged.
#   Android SDK reused at $HOME/android-sdk (the owner's smoke-test install); set
#   ANDROID_HOME to use another location. Packages are pinned by name, not version.
#   Markor APK downloaded from the GitHub release and verified against the SHA-256
#   pinned in harness/emulator/manager.py; a mismatch is a hard failure.
set -euo pipefail

LABS_DIR="${LABS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"   # the checkout this script lives in
ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
SYSTEM_IMAGE="${LABS_SYSTEM_IMAGE:-system-images;android-36.1;google_apis;x86_64}"
PY=python3.12
ENV_FILE="$HOME/labs-env.sh"

# Seed URL for a fresh SDK dir only; sdkmanager then upgrades itself to cmdline-tools;latest.
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"

MARKOR_FILE="net.gsantner.markor-v163-2.16.1-flavorDefault-release.apk"
MARKOR_URL="https://github.com/gsantner/markor/releases/download/v2.16.1/$MARKOR_FILE"
MARKOR_SHA256="e88cdcced7aa3dca25e6b9c7a9bdcfad3e3988ee545be951f42bf9441b5e46bf"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] error: %s\n' "$*" >&2; exit 1; }
STEP_LOG="${TMPDIR:-/tmp}/bootstrap_vm.log"; : >"$STEP_LOG"
# run_logged STEP cmd...: capture output; on failure name the step and show the tail.
run_logged() {
  local step="$1"; shift
  { printf '\n=== %s: %s\n' "$step" "$*"; "$@" </dev/null; } >>"$STEP_LOG" 2>&1 \
    || { tail -25 "$STEP_LOG" >&2; die "step '$step' failed (full log: $STEP_LOG)"; }
}

[ "$(uname -m)" = "x86_64" ] || die "expected x86_64, got $(uname -m)"
[ -e /dev/kvm ] || die "/dev/kvm missing: enable nested virtualization on the instance"

# --- 1. OS packages and Python 3.12 -----------------------------------------
export DEBIAN_FRONTEND=noninteractive
if ! command -v "$PY" >/dev/null 2>&1; then
  log "installing $PY from ppa:deadsnakes/ppa"
  run_logged "add-apt-repository deadsnakes" sudo add-apt-repository -y ppa:deadsnakes/ppa
  run_logged "apt-get update" sudo apt-get update -qq
  run_logged "apt-get install python3.12" sudo apt-get install -y -qq "$PY" "$PY-venv"
fi
log "python: $("$PY" --version)"
run_logged "apt-get install base packages" sudo apt-get install -y -qq --no-install-recommends git curl unzip openjdk-17-jdk-headless
log "java: $(java -version 2>&1 | head -1)"

if ! id -nG | tr ' ' '\n' | grep -qx kvm; then
  sudo usermod -aG kvm "$USER"
  log "added $USER to the kvm group; log out and back in before booting an emulator"
fi

# --- 2. Android SDK ---------------------------------------------------------
export ANDROID_HOME ANDROID_SDK_ROOT="$ANDROID_HOME"
SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
if [ ! -x "$SDKMANAGER" ]; then
  log "no sdkmanager under $ANDROID_HOME; seeding cmdline-tools"
  mkdir -p "$ANDROID_HOME/cmdline-tools"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/ct.zip" "$CMDLINE_TOOLS_URL"
  unzip -q "$tmp/ct.zip" -d "$tmp"
  rm -rf "$ANDROID_HOME/cmdline-tools/latest"
  mv "$tmp/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
  rm -rf "$tmp"
fi

image_rel="$(tr ';' '/' <<<"$SYSTEM_IMAGE")"
sdk_present() {
  [ -x "$ANDROID_HOME/platform-tools/adb" ] && [ -x "$ANDROID_HOME/emulator/emulator" ] && [ -f "$ANDROID_HOME/$image_rel/system.img" ]
}
if sdk_present && [ "${FORCE_SDK:-0}" != 1 ]; then
  log "SDK packages already on disk; skipping sdkmanager (FORCE_SDK=1 to re-run it)"
else
  log "checking that $SYSTEM_IMAGE exists in the repository"
  # cmdline-tools 23.0 prints package paths with '/' instead of ';'; normalise both forms.
  run_logged "sdkmanager --list" "$SDKMANAGER" --list
  available="$(awk '{print $1}' "$STEP_LOG" | tr '/' ';' | grep '^system-images;' | sort -u)"
  if ! grep -qxF "$SYSTEM_IMAGE" <<<"$available"; then
    log "NOT FOUND: $SYSTEM_IMAGE. Nearest x86_64 google_apis images:"
    grep -E 'android-3[5-9].*google_apis;x86_64' <<<"$available" | sed 's/^/    /'
    die "set LABS_SYSTEM_IMAGE to one of the above and re-run"
  fi
  yes 2>/dev/null | "$SDKMANAGER" --licenses >>"$STEP_LOG" 2>&1 || true
  log "installing cmdline-tools;latest platform-tools emulator $SYSTEM_IMAGE (log: $STEP_LOG)"
  run_logged "sdkmanager --install" "$SDKMANAGER" --install "cmdline-tools;latest" "platform-tools" "emulator" "$SYSTEM_IMAGE"
fi
# sdkmanager cannot replace the directory it runs from, so an upgrade of
# cmdline-tools;latest lands in cmdline-tools/latest-2; promote it on the next run.
if [ -d "$ANDROID_HOME/cmdline-tools/latest-2" ]; then
  log "promoting cmdline-tools/latest-2 to latest"
  rm -rf "$ANDROID_HOME/cmdline-tools/latest"
  mv "$ANDROID_HOME/cmdline-tools/latest-2" "$ANDROID_HOME/cmdline-tools/latest"
fi
# Validate on disk rather than by parsing sdkmanager's (version-dependent) listing.
[ -x "$ANDROID_HOME/platform-tools/adb" ] || die "platform-tools not installed"
[ -x "$ANDROID_HOME/emulator/emulator" ] || die "emulator not installed"
[ -f "$ANDROID_HOME/$image_rel/system.img" ] || die "$SYSTEM_IMAGE not installed under $ANDROID_HOME/$image_rel"
log "installed:"
for d in cmdline-tools/latest platform-tools emulator "$image_rel"; do
  printf '    %-50s %s\n' "$d" "$(grep -s '^Pkg.Revision=' "$ANDROID_HOME/$d/source.properties" | cut -d= -f2)"
done
"$ANDROID_HOME/emulator/emulator" -accel-check | sed 's/^/[bootstrap] accel: /'

# --- 3. Python venv with the pyproject deps only ----------------------------
if [ -f "$LABS_DIR/pyproject.toml" ]; then
  if [ ! -x "$LABS_DIR/.venv/bin/python" ]; then
    log "creating $LABS_DIR/.venv"
    "$PY" -m venv "$LABS_DIR/.venv"
  fi
  "$LABS_DIR/.venv/bin/pip" install -q --upgrade pip
  "$LABS_DIR/.venv/bin/pip" install -q -e "$LABS_DIR"   # dependencies from pyproject only
  log "venv: $("$LABS_DIR/.venv/bin/python" --version)"
else
  log "WARNING: $LABS_DIR/pyproject.toml missing; run deploy/push_source.sh first, then re-run"
fi

# --- 4. Pinned Markor APK ---------------------------------------------------
mkdir -p "$LABS_DIR/apks"
apk="$LABS_DIR/apks/$MARKOR_FILE"
if [ ! -f "$apk" ] || [ "$(sha256sum "$apk" | cut -d' ' -f1)" != "$MARKOR_SHA256" ]; then
  log "downloading Markor 2.16.1"
  curl -fsSL -o "$apk.part" "$MARKOR_URL"
  got="$(sha256sum "$apk.part" | cut -d' ' -f1)"
  [ "$got" = "$MARKOR_SHA256" ] || { rm -f "$apk.part"; die "Markor sha256 $got != pinned $MARKOR_SHA256"; }
  mv "$apk.part" "$apk"
fi
log "markor: $apk sha256 ok"

# --- 5. Environment file ----------------------------------------------------
cat >"$ENV_FILE" <<ENV
# written by deploy/bootstrap_vm.sh; source from an interactive shell.
# Only fills variables that are unset, so values given on a command line win. Not sourced by
# non-interactive gcloud --command shells: the deploy scripts source it themselves.
export ANDROID_HOME="\${ANDROID_HOME:-$ANDROID_HOME}"
export ANDROID_SDK_ROOT="\$ANDROID_HOME"
export LABS_SYSTEM_IMAGE="\${LABS_SYSTEM_IMAGE:-$SYSTEM_IMAGE}"
# The VM's one harness AVD (deploy/RUNBOOK.md §6b). Scripts derive LABS_DIR from their own location.
export LABS_AVD_NAME="\${LABS_AVD_NAME:-${LABS_AVD_NAME:-cloud_harness}}"
export LABS_AVD_PORT="\${LABS_AVD_PORT:-${LABS_AVD_PORT:-5584}}"
case ":\$PATH:" in *":\$ANDROID_HOME/platform-tools:"*) ;; *)
  export PATH="\$ANDROID_HOME/platform-tools:\$ANDROID_HOME/emulator:\$ANDROID_HOME/cmdline-tools/latest/bin:\$PATH" ;;
esac
ENV
grep -qF "labs-env.sh" "$HOME/.bashrc" || printf '\n[ -f "$HOME/labs-env.sh" ] && . "$HOME/labs-env.sh"\n' >>"$HOME/.bashrc"
log "wrote $ENV_FILE (sourced from ~/.bashrc)"
log "done"
