"""Add the sample app (com.labs.snackorders) to the harness emulator's baseline.

    python -m harness.emulator.provision_sample_app [--windowed] [--apk PATH] [--baseline-json PATH] [--upgrade]

Sequence: boot if needed, restore `baseline`, install the APK, confirm the
package and its orders provider answer, force-stop it, save `baseline`
again (the one sanctioned re-baseline), and record the APK's SHA-256 under
the "sample_app" key of baseline.json. The recording helper only adds keys:
an existing key with a different value is an error, never overwritten.
The one exception is --upgrade: it installs a different APK over the
recorded one and then replaces the "sample_app" entry (keeping the old hash
under "replaces_sha256"); nothing else in the file changes.

Uses the emulator manager's AVD, port and serial, so it follows whatever
the manager resolves (including its environment overrides).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.contracts import DeviceError
from harness.emulator import manager
from harness.seed.sample_app import PACKAGE, PROVIDER_ORDERS_URI

log = logging.getLogger(__name__)

DEFAULT_APK = manager.REPO / "sample-app" / "dist" / "snackorders-debug.apk"
BASELINE_KEY = "sample_app"


class BaselineKeyConflict(ValueError):
    """add_baseline_keys found an existing key with a different value."""


def add_baseline_keys(path: str | Path, new_keys: dict[str, Any]) -> dict[str, Any]:
    """Add keys to the baseline JSON object. A key that already exists with
    an equal value is left alone; one with a different value raises. Returns
    the resulting object."""
    p = Path(path)
    data = json.loads(p.read_text()) if p.is_file() else {}
    if not isinstance(data, dict):
        raise BaselineKeyConflict(f"{p} does not hold a JSON object")
    for key, value in new_keys.items():
        if key in data and data[key] != value:
            raise BaselineKeyConflict(f"{p}: key {key!r} already present with a different value")
        data[key] = value
    p.write_text(json.dumps(data, indent=2) + "\n")
    return data


def replace_baseline_key(path: str | Path, key: str, value: Any) -> dict[str, Any]:
    """Set one key of the baseline JSON object, overwriting it. Only the
    explicit --upgrade path uses this; returns the resulting object."""
    p = Path(path)
    data = json.loads(p.read_text()) if p.is_file() else {}
    if not isinstance(data, dict):
        raise BaselineKeyConflict(f"{p} does not hold a JSON object")
    data[key] = value
    p.write_text(json.dumps(data, indent=2) + "\n")
    return data


def sha256_of(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shell(*args: str, timeout: float = 30.0) -> str:
    return manager._shell(*args, timeout=timeout)


def installed_packages() -> set[str]:
    return {l.removeprefix("package:").strip() for l in _shell("pm", "list", "packages").splitlines() if l.strip()}


def installed_version(package: str = PACKAGE, packages: set[str] | None = None) -> str | None:
    """versionName from dumpsys, or None when the package is absent."""
    if package not in (installed_packages() if packages is None else packages):
        return None
    for line in _shell("dumpsys", "package", package).splitlines():
        line = line.strip()
        if line.startswith("versionName="):
            return line.split("=", 1)[1]
    return ""


def provider_answers() -> bool:
    """A fresh install has no orders, so "No result found." is a valid answer;
    rows are too. Anything else (empty output, errors) is not."""
    out = _shell("content", "query", "--uri", PROVIDER_ORDERS_URI, timeout=60)
    if "Error" in out or "Exception" in out:
        return False
    return "Row:" in out or "No result found" in out


def recorded_sample_app(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    data = json.loads(p.read_text())
    return data.get(BASELINE_KEY) if isinstance(data, dict) else None


def provision(apk: str | Path = DEFAULT_APK, windowed: bool = True, baseline_json: str | Path | None = None,
              upgrade: bool = False) -> dict:
    """Idempotent for the same APK: a repeat re-installs and re-saves the
    snapshot but keeps the existing record. A different APK than the one
    recorded is refused before the emulator is touched, unless `upgrade`:
    then it is installed over the old one (install -r keeps the app's data;
    the app migrates its database) and the record is replaced."""
    apk = Path(apk)
    if not apk.is_file():
        raise DeviceError(f"APK not found: {apk} (build it per sample-app/BUILD.md)")
    digest = sha256_of(apk)
    baseline_path = Path(manager.BASELINE_JSON if baseline_json is None else baseline_json)
    existing = recorded_sample_app(baseline_path)
    upgrading = existing is not None and existing.get("sha256") != digest
    if upgrading and not upgrade:
        raise BaselineKeyConflict(
            f"{baseline_path}: {BASELINE_KEY} already records sha256 {existing.get('sha256')}, not {digest}")
    boot_s = None
    if not manager._online():
        boot_s = manager.start(windowed=windowed)
    restore_s = manager.restore_snapshot(manager.BASELINE)
    manager.install_apk(apk, digest)
    packages = installed_packages()
    version = installed_version(packages=packages)
    if version is None:
        raise DeviceError(f"{PACKAGE} not listed after install")
    if not provider_answers():
        raise DeviceError(f"{PACKAGE}: orders provider did not answer")
    _shell("am", "force-stop", PACKAGE)
    _shell("input", "keyevent", "KEYCODE_HOME")
    time.sleep(1)
    manager.save_snapshot(manager.BASELINE)
    info = existing if existing is not None and not upgrading else {
        "package": PACKAGE,
        "apk": str(apk.relative_to(manager.REPO)) if apk.is_relative_to(manager.REPO) else str(apk),
        "sha256": digest,
        "version_name": version,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": PROVIDER_ORDERS_URI,
        # stock_package_count above is the pre-install inventory; this is the count now.
        "package_count_after_install": len(packages),
    }
    if upgrading:
        info["replaces_sha256"] = existing.get("sha256")
        replace_baseline_key(baseline_path, BASELINE_KEY, info)
    else:
        add_baseline_keys(baseline_path, {BASELINE_KEY: info})
    return {**info, "serial": manager.SERIAL, "avd": manager.AVD_NAME, "boot_s": boot_s, "restore_s": round(restore_s, 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.emulator.provision_sample_app",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--apk", default=str(DEFAULT_APK))
    ap.add_argument("--windowed", action="store_true", help="boot with a window if the emulator is not running")
    ap.add_argument("--baseline-json", help=f"record file to update (default {manager.BASELINE_JSON})")
    ap.add_argument("--upgrade", action="store_true",
                    help="install over a different recorded APK and replace its sample_app record")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(provision(a.apk, windowed=a.windowed, baseline_json=a.baseline_json, upgrade=a.upgrade), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
