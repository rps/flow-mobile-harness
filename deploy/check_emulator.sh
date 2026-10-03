#!/usr/bin/env bash
# Prove the x86_64 emulator path on the VM with a throwaway AVD, then delete it.
# Run ON the VM after bootstrap_vm.sh. Headless (-no-window) is correct here.
#
# Checks: AVD creation from $LABS_SYSTEM_IMAGE, headless boot time with KVM,
# adb root, the iptables REJECT of the host alias 10.0.2.2, the dispositive
# loopback probe (harness manager.loopback_reachable), snapshot save/load time.
#
# Everything goes through harness/emulator/manager.py. When the installed
# manager honours LABS_AVD_NAME / LABS_AVD_PORT / LABS_SYSTEM_IMAGE (area B's
# overrides) nothing is patched and the script reports mode=manager; on an
# older manager the same module attributes are set in-process before any call
# (mode=patched:<attrs>), so the AVD name, serial and image are always the
# ones requested here and never the manager's defaults.
#
# Overrides: LABS_AVD_NAME (cloud_check) LABS_AVD_PORT (5584)
#            LABS_SYSTEM_IMAGE (system-images;android-36.1;google_apis;x86_64)
#            LABS_DIR (~/labs) ANDROID_HOME (~/android-sdk) KEEP_AVD=1 keeps the AVD.
set -euo pipefail
# labs-env.sh only fills unset variables, so command-line overrides win.
[ -f "$HOME/labs-env.sh" ] && . "$HOME/labs-env.sh"
export LABS_DIR="${LABS_DIR:-$HOME/labs}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export LABS_AVD_NAME="${LABS_AVD_NAME:-cloud_check}"
export LABS_AVD_PORT="${LABS_AVD_PORT:-5584}"
export LABS_SYSTEM_IMAGE="${LABS_SYSTEM_IMAGE:-system-images;android-36.1;google_apis;x86_64}"
export KEEP_AVD="${KEEP_AVD:-0}"
export PYTHONPATH="$LABS_DIR"
PY="$LABS_DIR/.venv/bin/python"
ADB="$ANDROID_HOME/platform-tools/adb"
EMULATOR="$ANDROID_HOME/emulator/emulator"
SERIAL="emulator-$LABS_AVD_PORT"

[ -x "$PY" ] || { echo "no venv at $LABS_DIR/.venv (run bootstrap_vm.sh)" >&2; exit 1; }
# Refuse to touch anything that already exists: this script only ever deletes an AVD it created.
if "$EMULATOR" -list-avds | grep -qx "$LABS_AVD_NAME"; then
  echo "AVD $LABS_AVD_NAME already exists; refusing to reuse or delete it" >&2; exit 1
fi
if "$ADB" devices | grep -q "^$SERIAL[[:space:]]"; then
  echo "$SERIAL is already online; pick another LABS_AVD_PORT" >&2; exit 1
fi
printf 'RESULT accel=%s\n' "$("$EMULATOR" -accel-check 2>/dev/null | grep -E 'KVM|usable|not' | head -1)"

cd "$LABS_DIR"   # `python -` puts the cwd first on sys.path; make it this checkout, not wherever the caller was
exec "$PY" - <<'PYEOF'
import os, subprocess, time
from pathlib import Path
from harness.contracts import DeviceError
from harness.emulator import manager as m

name, port, image = os.environ["LABS_AVD_NAME"], int(os.environ["LABS_AVD_PORT"]), os.environ["LABS_SYSTEM_IMAGE"]
keep = os.environ.get("KEEP_AVD") == "1"
avd_dir = Path.home() / ".android" / "avd"

def result(k, v): print(f"RESULT {k}={v}", flush=True)
def exists(path):
    try: m._shell("ls", path); return True
    except DeviceError: return False

# --- make the manager target exactly this AVD/serial/image -------------------
patched = []
if m.AVD_NAME != name:
    m.AVD_NAME, m.TOP_INI, m.CONFIG_INI = name, avd_dir / f"{name}.ini", avd_dir / f"{name}.avd" / "config.ini"
    patched.append("AVD_NAME")
if m.PORT != port:
    m.PORT, m.SERIAL = port, f"emulator-{port}"
    patched.append("PORT")
if m.SYSTEM_IMAGE != image:
    m.SYSTEM_IMAGE, m.SYSTEM_IMAGE_DIR = image, Path(*image.split(";"))
    patched.append("SYSTEM_IMAGE")
# _online() binds the serial as a default argument at import; re-point it at the live value.
_orig_online = m._online
m._online = lambda serial=None: _orig_online(serial or m.SERIAL)
if "SYSTEM_IMAGE_DIR" not in m._image_api_level.__code__.co_names:
    # pre-override manager: _image_api_level hard-codes the arm64 path
    def _api_level():
        for line in (m.SDK / m.SYSTEM_IMAGE_DIR / "source.properties").read_text().splitlines():
            if line.startswith("AndroidVersion.ApiLevel="): return line.split("=", 1)[1].strip()
        raise DeviceError("no AndroidVersion.ApiLevel")
    m._image_api_level = _api_level
    patched.append("_image_api_level")
result("mode", "manager" if not patched else "patched:" + ",".join(patched))
assert m.SERIAL == f"emulator-{port}" and m.AVD_NAME == name and m.SYSTEM_IMAGE == image

created = False
try:
    t = time.monotonic(); created = m.create_avd(); result("create_s", f"{time.monotonic()-t:.1f}")
    assert created, "create_avd returned False although the AVD did not exist"
    for ini in (m.TOP_INI, m.CONFIG_INI):
        for line in ini.read_text().splitlines():
            if line.split("=")[0].strip() in ("target", "image.sysdir.1", *m.AVD_OVERRIDES):
                print(f"[check]   {ini.name}: {line}")
    result("boot_s", f"{m.start():.1f}")                      # windowed=False => -no-window
    result("abi", m._shell("getprop", "ro.product.cpu.abi").strip())
    result("api", m._shell("getprop", "ro.build.version.sdk").strip())

    root_ok, blocked = m.block_host_loopback()
    result("adb_root", root_ok)
    try:
        m._shell("iptables", "-C", "OUTPUT", "-d", m.HOST_ALIAS, "-j", "REJECT"); result("iptables_reject_present", True)
    except DeviceError:
        result("iptables_reject_present", False)
    result("loopback_blocked", blocked)
    result("loopback_reachable_probe", m.loopback_reachable())

    snap = "cloud_check_base"
    t = time.monotonic(); m.save_snapshot(snap); result("snapshot_save_s", f"{time.monotonic()-t:.1f}")
    m._shell("touch", "/data/local/tmp/dirty_marker")            # make the load observable
    result("snapshot_load_s", f"{m.restore_snapshot(snap):.1f}")
    result("state_reverted", not exists("/data/local/tmp/dirty_marker"))
    result("root_after_restore", m._shell("id", "-u").strip() == "0")
    result("loopback_blocked_after_restore", not m.loopback_reachable())
    result("disk_avd_mb", sum(f.stat().st_size for f in (avd_dir / f"{name}.avd").rglob("*") if f.is_file()) // 2**20)
finally:
    try: m.stop()
    except DeviceError as e: print(f"[check] stop: {e}")
    if created and not keep:
        subprocess.run([m.AVDMANAGER, "delete", "avd", "-n", name], capture_output=True)
        print(f"[check] deleted AVD {name}" if not (avd_dir / f"{name}.ini").exists() else f"[check] WARNING: {name} still present")
    elif created:
        print(f"[check] kept AVD {name} (KEEP_AVD=1)")
PYEOF
