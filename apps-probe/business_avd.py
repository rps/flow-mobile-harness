"""Stand-alone provisioning for the Play-image business AVD (Area D).

Drives avdmanager/emulator/adb directly, like the probe did, so it does not
depend on harness.emulator.manager. The harness profile module
(harness/emulator/profiles/business.py) is built on top of the manager once
its AVD overrides land; this script stays as the audit trail of what was run.

    python apps-probe/business_avd.py {create,start,stop,save,load,status,abi}

Applies the two documented avdmanager fixes: rewrite target=android-0 in the
top-level .ini (else HVF is disabled) and set disk.dataPartition.size=6G,
dropping the disk.dataPartition.path=<temp> line (else /data is 800M).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
ADB = str(SDK / "platform-tools" / "adb")
EMULATOR = str(SDK / "emulator" / "emulator")
AVDMANAGER = str(SDK / "cmdline-tools" / "latest" / "bin" / "avdmanager")

AVD_NAME = "p2_business_play"
PORT = 5592
SERIAL = f"emulator-{PORT}"
SYSTEM_IMAGE = "system-images;android-36.1;google_apis_playstore;arm64-v8a"
IMAGE_DIR = SDK / "system-images" / "android-36.1" / "google_apis_playstore" / "arm64-v8a"
DEVICE_PROFILE = "pixel_7"
AVD_DIR = Path.home() / ".android" / "avd"
TOP_INI = AVD_DIR / f"{AVD_NAME}.ini"
CONFIG_INI = AVD_DIR / f"{AVD_NAME}.avd" / "config.ini"
CONFIG_OVERRIDES = {"hw.ramSize": "2048M", "hw.gpu.enabled": "yes", "disk.dataPartition.size": "6G", "hw.keyboard": "yes"}
CONFIG_DROP = {"disk.dataPartition.path"}
SNAPSHOT = "business"
LOG = Path(os.environ.get("TMPDIR", "/tmp")) / f"{AVD_NAME}.log"

APPS = {
    "timecamp": "com.timecamp.mobile",
    "insightly": "com.insightly.droid",
    "invoice_ninja": "com.invoiceninja.app",
}


def run(cmd: list[str], timeout: float = 120, stdin: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)
    if check and proc.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def adb(*args: str, timeout: float = 60, check: bool = True) -> str:
    return run([ADB, "-s", SERIAL, *args], timeout=timeout, check=check)


def online() -> bool:
    return any(l.split() == [SERIAL, "device"] for l in run([ADB, "devices"]).splitlines())


def set_ini(ini: Path, values: dict[str, str], drop: set[str] = frozenset()) -> None:
    lines = [l for l in ini.read_text().splitlines() if l.split("=", 1)[0] not in values and l.split("=", 1)[0] not in drop]
    lines += [f"{k}={v}" for k, v in values.items()]
    ini.write_text("\n".join(lines) + "\n")


def api_level() -> str:
    for line in (IMAGE_DIR / "source.properties").read_text().splitlines():
        if line.startswith("AndroidVersion.ApiLevel="):
            return line.split("=", 1)[1].strip()
    sys.exit("no ApiLevel in source.properties")


def create() -> None:
    if AVD_NAME in run([EMULATOR, "-list-avds"]).split():
        print("exists")
    else:
        run([AVDMANAGER, "create", "avd", "-n", AVD_NAME, "-k", SYSTEM_IMAGE, "-d", DEVICE_PROFILE], stdin="no\n")
        print("created")
    before = TOP_INI.read_text()
    set_ini(TOP_INI, {"target": f"android-{api_level()}"})
    set_ini(CONFIG_INI, CONFIG_OVERRIDES, CONFIG_DROP)
    print("top ini had:", [l for l in before.splitlines() if l.startswith("target=")])
    print("top ini now:", [l for l in TOP_INI.read_text().splitlines() if l.startswith("target=")])
    print("config:", [l for l in CONFIG_INI.read_text().splitlines() if l.split("=")[0] in CONFIG_OVERRIDES or l.startswith("disk.dataPartition")])


def start(snapshot: str | None = None, wipe: bool = False, timeout: float = 300) -> None:
    if online():
        sys.exit(f"{SERIAL} already running")
    if "target=android-0" in TOP_INI.read_text().split():
        sys.exit(f"{TOP_INI} has target=android-0; run create first")
    cmd = [EMULATOR, "-avd", AVD_NAME, "-port", str(PORT), "-no-snapshot-save", "-no-boot-anim", "-no-audio"]
    if snapshot:
        cmd += ["-snapshot", snapshot]
    if wipe:
        cmd.append("-wipe-data")
    offset = LOG.stat().st_size if LOG.exists() else 0
    t0 = time.monotonic()
    with open(LOG, "ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    while time.monotonic() - t0 < timeout:
        tail = LOG.read_bytes()[offset:]
        if b"hvf is not enabled" in tail:
            proc.kill()
            sys.exit(f"emulator started without HVF; killed (log {LOG})")
        if proc.poll() is not None:
            sys.exit(f"emulator exited {proc.returncode} (log {LOG})")
        if online() and adb("shell", "getprop", "sys.boot_completed", check=False).strip() == "1":
            print(f"booted {SERIAL} in {time.monotonic() - t0:.1f}s; pid {proc.pid}; log {LOG}")
            return
        time.sleep(1)
    sys.exit("boot timeout")


def stop() -> None:
    if online():
        adb("emu", "kill")
        for _ in range(60):
            if not online():
                break
            time.sleep(1)
    print("stopped" if not online() else "still running")


def emu(*args: str) -> str:
    out = adb("emu", *args, timeout=180)
    if "KO" in out:
        sys.exit(f"emu {' '.join(args)}: {out.strip()}")
    return out


def status() -> None:
    print("online:", online())
    if online():
        print("boot_completed:", adb("shell", "getprop", "sys.boot_completed").strip())
        print("cpu implementer (0x61 = HVF):", [l for l in adb("shell", "cat", "/proc/cpuinfo").splitlines() if "implementer" in l][:1])
        print("df /data:", adb("shell", "df", "-h", "/data").splitlines()[-1])
        print("accounts:", adb("shell", "dumpsys", "account", check=False).count("Account {"))
        print("snapshots:", adb("emu", "avd", "snapshot", "list", check=False).strip())


def abi() -> None:
    """Which ABIs each installed app ships (for x86_64 cloud hosting)."""
    for name, pkg in APPS.items():
        dump = adb("shell", "pm", "dump", pkg, check=False)
        abis = sorted({l.strip() for l in dump.splitlines() if "abi" in l.lower() and "=" in l})
        splits = sorted({l.strip() for l in dump.splitlines() if "split" in l.lower() and ("config." in l or "splits=" in l)})
        paths = adb("shell", "pm", "path", pkg, check=False).strip().splitlines()
        print(f"== {name} ({pkg})")
        for l in abis + splits + paths:
            print("  ", l)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["create", "start", "stop", "save", "load", "status", "abi"])
    p.add_argument("--snapshot", default=None, help="start: cold-boot from this snapshot")
    p.add_argument("--wipe", action="store_true")
    p.add_argument("--name", default=SNAPSHOT, help="save/load: snapshot name")
    a = p.parse_args()
    if a.command == "create":
        create()
    elif a.command == "start":
        start(a.snapshot, a.wipe)
    elif a.command == "stop":
        stop()
    elif a.command == "save":
        print(emu("avd", "snapshot", "save", a.name).strip())
    elif a.command == "load":
        print(emu("avd", "snapshot", "load", a.name).strip())
    elif a.command == "status":
        status()
    elif a.command == "abi":
        abi()


if __name__ == "__main__":
    main()
