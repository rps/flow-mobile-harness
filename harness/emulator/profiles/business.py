"""Business AVD profile: Play-image emulator with TimeCamp, Insightly CRM and
Invoice Ninja logged in (AVD p2_business_play, port 5592, snapshot `business`).

The manager reads LABS_AVD_NAME / LABS_AVD_PORT / LABS_SYSTEM_IMAGE when it
is imported, so this module sets them and imports the manager lazily;
importing the manager for the default AVD first and then this profile raises
ProfileError. To use the stock CLI against this profile, prefix the command
with exactly these values (ENV_PREFIX below):

    LABS_AVD_NAME=p2_business_play LABS_AVD_PORT=5592 \
    LABS_SYSTEM_IMAGE=system-images;android-36.1;google_apis_playstore;arm64-v8a \
    python -m harness.cli run --task biz_b1_hours --confirm approve --windowed

Pass this module as `emulator` to runner.run_task, or call run_scored():
restore_snapshot() always loads `business` (the runner asks for "baseline"
by name), SERIAL is pinned here, and every gate fails closed: freeform goals
are refused outright, and a run is refused unless business.json exists, is
readable and records this AVD with freeform disabled.

Differences from the default profile, all recorded in business.json:
no root (Play image), so host loopback cannot be blocked and freeform runs
stay refused; the AVD is created by apps-probe/business_avd.py (the manager
only knows the google_apis image); the three apps keep their data on vendor
servers, so a snapshot restore does not undo an invoice the agent created.

Invoice oracle: with INVOICE_NINJA_API_KEY and INVOICE_NINJA_ENDPOINT in
os.environ (export them in the shell; nothing here reads .env), invoices are
read from the Invoice Ninja API (tier 2) and run_scored deletes the test
client's earlier invoices before a biz_d_invoice run, recording them in
meta["invoice_cleanup"]; meta["oracle"] says which oracle each value came from.

CLI: python -m harness.emulator.profiles.business
     {start,stop,restore,status,record-baseline,env,cleanup-invoices}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from harness.contracts import DeviceError
from harness.device.adb_shell import adb, shell

log = logging.getLogger(__name__)

AVD_NAME = "p2_business_play"
PORT = 5592
SERIAL = f"emulator-{PORT}"
SNAPSHOT = "business"
SYSTEM_IMAGE = "system-images;android-36.1;google_apis_playstore;arm64-v8a"
FREEFORM_ALLOWED = False
HERE = Path(__file__).resolve().parent
BASELINE_JSON = HERE / "business.json"
SCORED_BLOCKED_PACKAGES = ("com.android.vending",)
ENV = {"LABS_AVD_NAME": AVD_NAME, "LABS_AVD_PORT": str(PORT), "LABS_SYSTEM_IMAGE": SYSTEM_IMAGE}
ENV_PREFIX = " ".join(f"{k}={shlex.quote(v)}" for k, v in ENV.items())
APPS = {
    "timecamp": "com.timecamp.mobile",
    "insightly": "com.insightly.droid",
    "invoice_ninja": "com.invoiceninja.app",
    "markor": "net.gsantner.markor",
}
INVOICE_TASKS = ("biz_d_invoice", "biz_h")  # tasks whose oracle tier follows the invoice oracle
CLEANUP_TASKS = ("biz_d_invoice",)  # tasks that need no earlier invoice for the client


class ProfileError(DeviceError):
    """The manager is bound to a different AVD or does not support overrides."""


def manager() -> ModuleType:
    """harness.emulator.manager bound to this AVD (imported on first use)."""
    existing = sys.modules.get("harness.emulator.manager")
    if existing is not None and getattr(existing, "AVD_NAME", None) != AVD_NAME:
        raise ProfileError(f"harness.emulator.manager is already bound to {existing.AVD_NAME!r}; "
                           f"import {__name__} before the manager or run in a separate process")
    os.environ.update(ENV)
    from harness.emulator import manager as m

    if m.AVD_NAME != AVD_NAME or m.PORT != PORT or getattr(m, "SYSTEM_IMAGE", SYSTEM_IMAGE) != SYSTEM_IMAGE:
        raise ProfileError("harness.emulator.manager ignores LABS_AVD_NAME/LABS_AVD_PORT/LABS_SYSTEM_IMAGE")
    return m


def online() -> bool:
    return manager()._online()


def start(windowed: bool = True, timeout: float = 300.0) -> float:
    """Boot the AVD (windowed by default, so the owner can watch)."""
    return manager().start(windowed=windowed, timeout=timeout)


def stop() -> None:
    manager().stop()


def restore_snapshot(name: str | None = None, timeout: float = 120.0) -> float:
    """Load snapshot `business`. The runner passes "baseline"; any other
    name is a caller error."""
    if name not in (None, SNAPSHOT, "baseline"):
        raise ProfileError(f"the business profile has only snapshot {SNAPSHOT!r}, not {name!r}")
    return manager().restore_snapshot(SNAPSHOT, timeout=timeout)


def save_snapshot() -> None:
    """Re-save `business` from the running state. Only for deliberate re-provisioning."""
    manager().save_snapshot(SNAPSHOT)


def baseline_info(path: str | Path = BASELINE_JSON) -> dict:
    """business.json, checked: it must exist, parse, name this AVD and
    serial, and say freeform is not allowed. Anything else raises
    ProfileError, so a missing or stale record can never permit a run."""
    try:
        info = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read profile baseline {path}: {exc}") from exc
    if not isinstance(info, dict):
        raise ProfileError(f"profile baseline {path} is not a JSON object")
    for key, want in (("avd", AVD_NAME), ("serial", SERIAL), ("snapshot", SNAPSHOT), ("freeform_allowed", False)):
        if info.get(key) != want:
            raise ProfileError(f"profile baseline {path}: {key}={info.get(key)!r}, expected {want!r}")
    if info.get("host_loopback_blocked") is not False:
        raise ProfileError(f"profile baseline {path}: host_loopback_blocked must be recorded as false on this profile")
    return info


def assert_scored(task_id: str | None, goal: str | None, baseline_json: str | Path = BASELINE_JSON) -> None:
    """Refuse anything but a registered task on a verified baseline."""
    if goal is not None or not task_id:
        raise ProfileError("freeform runs are refused on the business profile (no root, host loopback reachable)")
    baseline_info(baseline_json)


def _shell(*args: str) -> str:
    return shell(SERIAL, *args)


def _app_versions() -> dict[str, dict[str, str]]:
    out = {}
    for name, pkg in APPS.items():
        dump = _shell("dumpsys", "package", pkg)
        info = {"package": pkg}
        for line in dump.splitlines():
            line = line.strip()
            for key in ("versionName", "installerPackageName", "primaryCpuAbi"):
                if line.startswith(key + "="):
                    info[key] = line.split("=", 1)[1]
        out[name] = info
    return out


def record_baseline() -> dict:
    """Write business.json from the running emulator. Credentials are never
    read; the Google account is recorded as present/absent only."""
    m = manager()
    try:
        root_out = adb(SERIAL, "root")
    except DeviceError as exc:
        root_out = str(exc)
    root = "cannot run as root" not in root_out and _shell("id", "-u").strip() == "0"
    reachable = m.loopback_reachable()
    if not root and not reachable:
        # Without root nothing can block the loopback; an unreachable probe means
        # the probe itself failed (e.g. nc missing), never that the block works.
        raise ProfileError("loopback probe failed on an unrooted image; refusing to record a block")
    info = {
        "avd": AVD_NAME,
        "serial": SERIAL,
        "snapshot": SNAPSHOT,
        "system_image": SYSTEM_IMAGE,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hvf": m.hvf_enabled(),
        "root": root,
        "host_loopback_blocked": bool(root) and not reachable,
        "host_loopback_reachable": reachable,
        "freeform_allowed": FREEFORM_ALLOWED,
        "google_account_present": "type=com.google" in _shell("dumpsys", "account"),
        "apps": _app_versions(),
        "markor_notebook_dir": "/storage/emulated/0/Documents/markor",
        "snapshots": [l.split()[1] for l in adb(SERIAL, "emu", "avd", "snapshot", "list").splitlines()
                      if l.strip().startswith("--")],
    }
    BASELINE_JSON.write_text(json.dumps(info, indent=2) + "\n")
    return info


def cleanup_invoices(api=None) -> dict:
    """Delete (soft, via the API) every not-yet-deleted invoice of the test
    client CLIENT_ORG, so biz_d_invoice starts with none; other clients'
    invoices are never touched. Raises ProfileError without API credentials."""
    from harness.verify import biz_api
    from harness.verify.biz_readback import CLIENT_ORG

    api = api if api is not None else biz_api.from_env()
    if api is None:
        raise ProfileError(f"invoice cleanup needs {biz_api.ENV_KEY} and {biz_api.ENV_ENDPOINT} in the environment")
    return biz_api.cleanup_client_invoices(api, CLIENT_ORG)


def oracle_meta(api) -> dict:
    """Which oracle each business value comes from, for run meta."""
    from harness.verify.biz_readback import REPORT_END, REPORT_START

    return {"invoices": "invoice_ninja_api" if api is not None else "invoice_ninja_screen_readback",
            "invoices_tier": 2 if api is not None else 5,
            "hours": "timecamp_screen_readback", "hours_range": f"{REPORT_START.isoformat()}..{REPORT_END.isoformat()}",
            "rate": "insightly_screen_readback"}


def env(config, windowed: bool = True, invoice_api=None):
    """harness.cli.Env for this profile: boots the AVD if needed and pairs the
    agent device with a BusinessInspector (tier-5 read-back; invoices from
    `invoice_api`, else from the API when the environment configures it,
    else from the screen). Refuses to build when business.json is missing or
    inconsistent. Prefer run_scored(), which also pins baseline_json so the
    runner never consults the default profile's record."""
    from harness.cli import Env
    from harness.device.adb import AdbDevice
    from harness.verify import biz_api
    from harness.verify.biz_readback import BusinessInspector

    baseline_info()
    if invoice_api is None:
        invoice_api = biz_api.from_env()
    if not online():
        log.info("booted %s in %.1fs", SERIAL, start(windowed=windowed))
    return Env(
        config=config,
        emulator=sys.modules[__name__],
        device_factory=lambda: AdbDevice(SERIAL, config),
        inspector_factory=lambda: BusinessInspector(SERIAL, invoice_api=invoice_api),
    )


def run_scored(task_id: str, config, confirm_policy, store, *, windowed: bool = True, **kwargs):
    """runner.run_task for one registered task on this profile. Freeform goals
    cannot be passed; baseline_json is always this profile's record.
    The invoice oracle (API or screen) is chosen from the environment; a task
    whose declared tier disagrees with it (environment changed after the
    task modules were imported) is refused. For biz_d_invoice with the API
    configured, the client's earlier invoices are deleted when the runner
    builds the inspector: after the snapshot restore, under the serial lock,
    and never for a run cancelled while queued. The result lands in
    meta["invoice_cleanup"] (the runner copies meta shallowly, so the dict
    placed there is the one run.json records); a failed cleanup ends the run
    with termination ERROR at stage "seed"."""
    from harness.runner import run_task
    from harness.tasks import registry
    from harness.verify import biz_api

    assert_scored(task_id, None)
    api = biz_api.from_env()
    task = registry.get(task_id)
    want = biz_api.invoice_oracle_tier()
    if task_id in INVOICE_TASKS and task.oracle_tier != want:
        raise ProfileError(f"{task_id} declares oracle tier {int(task.oracle_tier)} but the environment selects "
                           f"tier {int(want)}; set or unset {biz_api.ENV_KEY}/{biz_api.ENV_ENDPOINT} before starting")
    meta = kwargs.setdefault("meta", {})
    meta["profile"] = "business"
    meta["oracle"] = oracle_meta(api)
    e = env(config, windowed=windowed, invoice_api=api)
    inspector_factory = e.inspector_factory
    if task_id in CLEANUP_TASKS:
        record = {"status": "skipped: invoice API not configured"} if api is None else {"status": "pending"}
        meta["invoice_cleanup"] = record

        def inspector_factory(base=e.inspector_factory):
            if api is not None:
                try:
                    result = cleanup_invoices(api)
                except Exception as exc:
                    record["status"] = f"failed: {type(exc).__name__}: {exc}"
                    raise
                record.update(result, status="done")
            return base()
    kwargs.pop("goal", None)
    kwargs["baseline_json"] = BASELINE_JSON
    kwargs["windowed"] = windowed  # the runner's restore-retry cold boot uses it
    # Scored runs must not wander into the Play Store (installs, account prompts).
    kwargs["block_packages"] = (*SCORED_BLOCKED_PACKAGES, *kwargs.get("block_packages", ()))
    return run_task(task_id, None, config, confirm_policy, device_factory=e.device_factory,
                    inspector_factory=inspector_factory, emulator=e.emulator, store=store, **kwargs)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m harness.emulator.profiles.business")
    p.add_argument("command", choices=["start", "stop", "restore", "status", "record-baseline", "env",
                                       "cleanup-invoices"])
    p.add_argument("--no-window", action="store_true", help="boot headless (default is windowed)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.command == "start":
        print(f"boot_s={start(windowed=not a.no_window):.1f}")
    elif a.command == "stop":
        stop()
    elif a.command == "restore":
        print(f"restore_s={restore_snapshot():.2f}")
    elif a.command == "status":
        print(json.dumps({"serial": SERIAL, "online": online(), "snapshot": SNAPSHOT,
                          "baseline_json": str(BASELINE_JSON), "baseline_exists": BASELINE_JSON.exists()}, indent=2))
    elif a.command == "record-baseline":
        print(json.dumps(record_baseline(), indent=2))
    elif a.command == "env":
        print(ENV_PREFIX)
    elif a.command == "cleanup-invoices":
        print(json.dumps(cleanup_invoices(), indent=2))


if __name__ == "__main__":
    main()
