"""Provision and control the harness emulator (AVD p2_harness_api36 by default;
override with LABS_AVD_NAME / LABS_AVD_PORT).

Only this AVD is ever created, booted or killed; all adb calls pin its serial.
Runs restore the `baseline` snapshot and never save back. save_snapshot is only
called by provision_baseline (and the explicit `rebaseline` CLI command).

CLI: python -m harness.emulator.manager {create,start,stop,provision,restore,rebaseline,check-loopback}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.contracts import DeviceError
from harness.device.adb_shell import ADB, SDK, adb

log = logging.getLogger(__name__)

AVD_NAME = os.environ.get("LABS_AVD_NAME", "p2_harness_api36")


def _system_image_from_env() -> str:
    """LABS_SYSTEM_IMAGE: an sdkmanager package id of the shape
    system-images;<api>;<tag>;<abi>. Default: the arm64 google_apis image."""
    image = os.environ.get("LABS_SYSTEM_IMAGE", "system-images;android-36.1;google_apis;arm64-v8a")
    parts = image.split(";")
    if len(parts) != 4 or parts[0] != "system-images" or not all(parts):
        raise DeviceError(f"LABS_SYSTEM_IMAGE={image!r} must look like system-images;<api>;<tag>;<abi>")
    return image


SYSTEM_IMAGE = _system_image_from_env()
# SDK path of the image's package directory, e.g. system-images/android-36.1/google_apis/arm64-v8a
SYSTEM_IMAGE_DIR = Path(*SYSTEM_IMAGE.split(";"))
DEVICE_PROFILE = "pixel_7"
AVD_OVERRIDES = {"hw.ramSize": "2048M", "hw.gpu.enabled": "yes", "disk.dataPartition.size": "6G", "hw.keyboard": "yes"}
TOP_INI = Path.home() / ".android" / "avd" / f"{AVD_NAME}.ini"
CONFIG_INI = Path.home() / ".android" / "avd" / f"{AVD_NAME}.avd" / "config.ini"


def _port_from_env() -> int:
    """LABS_AVD_PORT: an even emulator console port in 5554..5682 (the
    emulator's range; adb talks to console port + 1). Default 5584."""
    raw = os.environ.get("LABS_AVD_PORT", "5584")
    try:
        port = int(raw)
    except ValueError:
        raise DeviceError(f"LABS_AVD_PORT={raw!r} is not an integer") from None
    if port % 2 or not 5554 <= port <= 5682:
        raise DeviceError(f"LABS_AVD_PORT={port} must be an even port in 5554..5682")
    return port


PORT = _port_from_env()  # 5554/5580 are used by other emulators on this host
SERIAL = f"emulator-{PORT}"
BASELINE = "baseline"
HOST_ALIAS = "10.0.2.2"

EMULATOR = str(SDK / "emulator" / "emulator")
AVDMANAGER = str(SDK / "cmdline-tools" / "latest" / "bin" / "avdmanager")

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STOCK_APPS = HERE / "stock_apps.txt"
BASELINE_JSON = HERE / "baseline.json"

MARKOR_PACKAGE = "net.gsantner.markor"
MARKOR_VERSION = "2.16.1"
MARKOR_APK = REPO / "apks" / "net.gsantner.markor-v163-2.16.1-flavorDefault-release.apk"
MARKOR_SHA256 = "e88cdcced7aa3dca25e6b9c7a9bdcfad3e3988ee545be951f42bf9441b5e46bf"
# Markor's default notebook (pref file_browser_last_browsed_folder after first launch).
# Exists, empty, in the baseline; Markor has no all-files access until granted.
MARKOR_NOTEBOOK = "/storage/emulated/0/Documents/markor"


def _run(cmd: list[str], timeout: float = 120.0, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise DeviceError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _shell(*args: str, timeout: float = 30.0) -> str:
    return adb(SERIAL, "shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)


def _online(serial: str = SERIAL) -> bool:
    out = _run([ADB, "devices"], timeout=15)
    return any(line.split() == [serial, "device"] for line in out.splitlines())


def _wait_responsive(timeout: float) -> None:
    """Wait until the device is online and reports boot completed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _online() and _shell("getprop", "sys.boot_completed", timeout=10).strip() == "1":
                return
        except DeviceError:
            pass
        time.sleep(0.5)
    raise DeviceError(f"{SERIAL} not responsive after {timeout}s")


# --- AVD lifecycle -----------------------------------------------------------


def create_avd() -> bool:
    """Create the AVD if missing. Returns True if it was created."""
    if AVD_NAME in _run([EMULATOR, "-list-avds"]).split():
        return False
    _run(
        [AVDMANAGER, "create", "avd", "-n", AVD_NAME, "-k", SYSTEM_IMAGE, "-d", DEVICE_PROFILE],
        stdin="no\n",  # "Do you wish to create a custom hardware profile?"
    )
    # avdmanager (cmdline-tools 12.0) writes target=android-0; the emulator then
    # reads API 3, disables HVF and falls back to software emulation.
    _set_ini_keys(TOP_INI, {"target": f"android-{_image_api_level()}"})
    _set_ini_keys(CONFIG_INI, AVD_OVERRIDES)
    return True


def _image_api_level() -> str:
    props = SDK / SYSTEM_IMAGE_DIR / "source.properties"
    for line in props.read_text().splitlines():
        if line.startswith("AndroidVersion.ApiLevel="):
            return line.split("=", 1)[1].strip()
    raise DeviceError(f"no AndroidVersion.ApiLevel in {props}")


def _set_ini_keys(ini: Path, values: dict[str, str]) -> None:
    lines = [l for l in ini.read_text().splitlines() if l.split("=", 1)[0] not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ini.write_text("\n".join(lines) + "\n")


def start(windowed: bool = False, timeout: float = 300.0) -> float:
    """Boot the AVD and wait for boot completed. Returns boot seconds.

    -no-snapshot-save: quitting never writes the quickboot snapshot, so the
    only way state persists is an explicit save_snapshot. Fails fast if the
    emulator reports that HVF (hardware acceleration) is off.
    """
    if _online():
        raise DeviceError(f"{SERIAL} is already running")
    if "target=android-0" in TOP_INI.read_text().split():
        raise DeviceError(f"{TOP_INI} has target=android-0; HVF would be disabled. Run create_avd's fix or edit it.")
    cmd = [EMULATOR, "-avd", AVD_NAME, "-port", str(PORT), "-no-snapshot-save", "-no-boot-anim", "-no-audio"]
    if not windowed:
        cmd.append("-no-window")
    logfile = Path(tempfile.gettempdir()) / f"{AVD_NAME}.log"
    offset = logfile.stat().st_size if logfile.exists() else 0
    t0 = time.monotonic()
    with open(logfile, "ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = t0 + timeout
    while True:
        with open(logfile, "rb") as fh:
            fh.seek(offset)
            if b"hvf is not enabled" in fh.read():
                proc.kill()
                raise DeviceError(f"emulator started without HVF; killed it (log: {logfile})")
        if proc.poll() is not None:
            raise DeviceError(f"emulator exited with {proc.returncode} (log: {logfile})")
        try:
            _wait_responsive(min(2.0, max(0.1, deadline - time.monotonic())))
            break
        except DeviceError:
            if time.monotonic() >= deadline:
                raise
    took = time.monotonic() - t0
    log.info("booted %s in %.1fs with HVF (log: %s)", SERIAL, took, logfile)
    return took


def hvf_enabled() -> bool:
    """True if the running emulator appears to use hardware acceleration.

    qemu's argv does not show the accelerator. Under HVF the guest sees the
    host CPU's ID register, so /proc/cpuinfo reports Apple's implementer 0x61;
    the latest start log must also lack "hvf is not enabled".
    """
    logfile = Path(tempfile.gettempdir()) / f"{AVD_NAME}.log"
    if logfile.exists():
        last = logfile.read_text(errors="replace").rsplit("Monitoring duration of emulator setup", 1)[-1]
        if "hvf is not enabled" in last:
            return False
    return "CPU implementer\t: 0x61" in _shell("cat", "/proc/cpuinfo")


def stop(timeout: float = 60.0) -> None:
    if not _online():
        return
    adb(SERIAL, "emu", "kill")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if SERIAL not in _run([ADB, "devices"], timeout=15):
            return
        time.sleep(0.5)
    raise DeviceError(f"{SERIAL} did not stop within {timeout}s")


def install_apk(path: str | Path, sha256: str | None = None) -> None:
    path = Path(path)
    if sha256 is not None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sha256:
            raise DeviceError(f"{path.name}: sha256 {digest} != pinned {sha256}")
    out = adb(SERIAL, "install", "-r", str(path), timeout=180)
    if "Success" not in out:
        raise DeviceError(f"install {path.name}: {out.strip()}")


# --- Snapshots ---------------------------------------------------------------


def _emu(*args: str, timeout: float = 120.0) -> str:
    out = adb(SERIAL, "emu", *args, timeout=timeout)
    if "KO" in out:
        raise DeviceError(f"emu {' '.join(args)}: {out.strip()}")
    return out


def save_snapshot(name: str) -> None:
    """Save the running state. Only for deliberate (re-)baselining."""
    _emu("avd", "snapshot", "save", name)
    log.info("saved snapshot %s", name)


def restore_snapshot(name: str = BASELINE, timeout: float = 120.0) -> float:
    """Load a snapshot (never saves). Returns seconds until the device responds."""
    t0 = time.monotonic()
    _emu("avd", "snapshot", "load", name, timeout=timeout)
    _wait_responsive(timeout)
    took = time.monotonic() - t0
    log.info("restored snapshot %s in %.2fs", name, took)
    return took


# --- Host loopback block -----------------------------------------------------


def loopback_reachable(timeout_s: int = 3) -> bool:
    """True if the device can open a TCP connection to the host via 10.0.2.2.

    A throwaway listener on host 127.0.0.1 makes the probe dispositive: a
    refused or timed-out connect means the block works, not that nothing listens.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(timeout_s + 5)
    port = srv.getsockname()[1]
    hits: list[bool] = []

    def accept() -> None:
        try:
            conn, _ = srv.accept()
            hits.append(True)
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        _shell("nc", "-z", "-w", str(timeout_s), HOST_ALIAS, str(port), timeout=timeout_s + 10)
    except DeviceError:
        pass  # nc exits non-zero when the connect fails
    t.join(timeout_s + 6)
    srv.close()
    return bool(hits)


def block_host_loopback() -> tuple[bool, bool]:
    """Try adb root + an iptables REJECT rule for 10.0.2.2. Returns (root_ok, blocked)."""
    try:
        adb(SERIAL, "root")
        time.sleep(1)
        _wait_responsive(60)
        root_ok = _shell("id", "-u").strip() == "0"
    except DeviceError as exc:
        log.warning("adb root failed: %s", exc)
        return False, False
    if not root_ok:
        return False, False
    rule = ["OUTPUT", "-d", HOST_ALIAS, "-j", "REJECT"]
    try:
        _shell("iptables", "-C", *rule)
    except DeviceError:
        try:
            _shell("iptables", "-I", *rule)
        except DeviceError as exc:
            log.warning("iptables failed: %s", exc)
            return True, False
    return True, not loopback_reachable()


# --- Baseline ----------------------------------------------------------------


def record_stock_apps() -> list[str]:
    pkgs = sorted(line.removeprefix("package:").strip() for line in _shell("pm", "list", "packages").splitlines() if line.strip())
    STOCK_APPS.write_text("\n".join(pkgs) + "\n")
    return pkgs


def observe_markor_first_launch(out_dir: Path) -> None:
    """Launch Markor once, save what appears, then clear its data so the
    baseline keeps the first-launch experience intact."""
    from harness.contracts import Config
    from harness.device.adb import AdbDevice

    dev = AdbDevice(SERIAL, Config())
    out_dir.mkdir(parents=True, exist_ok=True)
    dev.open_app(MARKOR_PACKAGE)
    time.sleep(5)
    (out_dir / "markor_first_launch.png").write_bytes(dev.screenshot().png)
    (out_dir / "markor_first_launch.txt").write_text(dev.ui_tree())
    _shell("am", "force-stop", MARKOR_PACKAGE)
    _shell("pm", "clear", MARKOR_PACKAGE)
    dev.home()


def provision_baseline(windowed: bool = False) -> dict:
    """Create, boot, inventory, install Markor, block loopback, save `baseline`."""
    create_avd()
    boot_s = start(windowed=windowed) if not _online() else None
    stock = record_stock_apps()
    install_apk(MARKOR_APK, MARKOR_SHA256)
    observe_markor_first_launch(REPO / "runs" / "provision")
    root_ok, blocked = block_host_loopback()
    if not blocked:
        log.warning("HOST LOOPBACK NOT BLOCKED (root=%s): the device can reach the host via %s", root_ok, HOST_ALIAS)
    info = {
        "avd": AVD_NAME,
        "serial": SERIAL,
        "snapshot": BASELINE,
        "system_image": SYSTEM_IMAGE,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hvf": hvf_enabled(),
        "root": root_ok,
        "host_loopback_blocked": blocked,
        "markor": {"package": MARKOR_PACKAGE, "version": MARKOR_VERSION, "sha256": MARKOR_SHA256, "notebook_dir": MARKOR_NOTEBOOK},
        "stock_package_count": len(stock),
    }
    save_snapshot(BASELINE)
    BASELINE_JSON.write_text(json.dumps(info, indent=2) + "\n")
    info["boot_s"] = boot_s
    return info


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m harness.emulator.manager")
    p.add_argument("command", choices=["create", "start", "stop", "provision", "restore", "rebaseline", "check-loopback"])
    p.add_argument("--windowed", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.command == "create":
        print("created" if create_avd() else "exists")
    elif a.command == "start":
        print(f"boot_s={start(windowed=a.windowed):.1f}")
    elif a.command == "stop":
        stop()
    elif a.command == "provision":
        print(json.dumps(provision_baseline(windowed=a.windowed), indent=2))
    elif a.command == "restore":
        print(f"restore_s={restore_snapshot(BASELINE):.2f}")
    elif a.command == "rebaseline":
        save_snapshot(BASELINE)
    elif a.command == "check-loopback":
        print(f"host_loopback_reachable={loopback_reachable()}")


if __name__ == "__main__":
    main()
