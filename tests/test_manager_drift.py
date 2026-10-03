"""manager.py fail-closed checks: AVD config, running image, snapshots, baseline."""

import json

import pytest

from harness.contracts import DeviceError
from harness.emulator import manager

SAMPLE_SHA = "a" * 64
IMG_DIR = manager.SYSTEM_IMAGE_DIR.as_posix()
API = "36.1"


@pytest.fixture
def avd_files(tmp_path, monkeypatch):
    top = tmp_path / f"{manager.AVD_NAME}.ini"
    cfg = tmp_path / f"{manager.AVD_NAME}.avd" / "config.ini"
    cfg.parent.mkdir()
    top.write_text(f"path={cfg.parent}\ntarget=android-{API}\n")
    cfg.write_text(f"abi.type=arm64-v8a\nimage.sysdir.1={IMG_DIR}/\n"
                   + "".join(f"{k}={v}\n" for k, v in manager.AVD_OVERRIDES.items()))
    monkeypatch.setattr(manager, "TOP_INI", top)
    monkeypatch.setattr(manager, "CONFIG_INI", cfg)
    monkeypatch.setattr(manager, "_image_api_level", lambda: API)
    return top, cfg


class Device:
    """Canned replies for _shell / _emu / _run."""

    def __init__(self, monkeypatch, *, avd=None, sdk="36", abi="arm64-v8a", snapshots=("baseline",),
                 apk="/data/app/x/base.apk", sha=manager.MARKOR_SHA256, list_avds=None,
                 sample_apk="/data/app/s/base.apk", sample_sha=SAMPLE_SHA, sample_package=None):
        self.avd = manager.AVD_NAME if avd is None else avd
        self.sample_apk, self.sample_sha = sample_apk, sample_sha
        self.shell_calls: list[tuple] = []
        self.sample_package = sample_package or manager.SAMPLE_APP_PACKAGE
        self.sdk, self.abi, self.snapshots, self.apk, self.sha = sdk, abi, list(snapshots), apk, sha
        self.list_avds = [manager.AVD_NAME] if list_avds is None else list_avds
        self.emu_calls: list[tuple] = []
        monkeypatch.setattr(manager, "_shell", self.shell)
        monkeypatch.setattr(manager, "adb", self.adb)
        monkeypatch.setattr(manager, "_run", self.run)
        monkeypatch.setattr(manager, "_wait_responsive", lambda timeout: None)

    def shell(self, *args, timeout=30.0):
        self.shell_calls.append(args)
        if args == ("getprop", "ro.build.version.sdk"):
            return self.sdk + "\n"
        if args == ("getprop", "ro.product.cpu.abi"):
            return self.abi + "\n"
        if args[:2] == ("pm", "path"):
            apk = {manager.MARKOR_PACKAGE: self.apk, self.sample_package: self.sample_apk}[args[2]]
            if not apk:
                raise DeviceError(f"adb shell pm path {args[2]} failed (1): ")
            return f"package:{apk}\n"
        if args[0] == "sha256sum":
            sha = {self.apk: self.sha, self.sample_apk: self.sample_sha}[args[1]]
            return f"{sha}  {args[1]}\n"
        raise AssertionError(args)

    def adb(self, serial, *args, timeout=120.0, **kw):
        assert serial == manager.SERIAL and args[0] == "emu"
        self.emu_calls.append(args[1:])
        if args[1:] == ("avd", "name"):
            return f"{self.avd}\r\nOK\r\n"
        if args[1:] == ("avd", "snapshot", "list"):
            if not self.snapshots:
                return "There is no snapshot available\r\nOK\r\n"
            rows = "".join(f"--        {n}                68M 2026-10-02 20:21:34   00:02:10.074\r\n" for n in self.snapshots)
            return ("List of snapshots present on all disks:\r\n"
                    "ID        TAG                 VM SIZE                DATE       VM CLOCK\r\n" + rows
                    + "\r\nList of partial (non-loadable) snapshots on 'cache.img':\r\n"
                    "ID        TAG                 VM SIZE                DATE       VM CLOCK\r\n"
                    "--        broken              1M 2026-10-02 20:21:34   00:00:01.000\r\n" + "OK\r\n")
        if args[1:4] == ("avd", "snapshot", "load"):
            return "OK\r\n" if args[4] in self.snapshots else "KO: snapshot not found\r\n"
        raise AssertionError(args)

    def run(self, cmd, timeout=120.0, stdin=None):
        if cmd[1:] == ["-list-avds"]:
            return "\n".join(self.list_avds) + "\n"
        raise AssertionError(cmd)


# --- create_avd / check_avd_config ------------------------------------------------


def test_existing_matching_avd_is_accepted(avd_files, monkeypatch):
    Device(monkeypatch)
    assert manager.create_avd() is False


@pytest.mark.parametrize("edit,needle", [
    (("config", "image.sysdir.1", "system-images/android-35/google_apis/arm64-v8a/"), "image.sysdir.1"),
    (("config", "disk.dataPartition.size", "2G"), "disk.dataPartition.size='2G'"),
    (("config", "hw.gpu.enabled", None), "hw.gpu.enabled=None"),
    (("top", "target", "android-0"), "target='android-0'"),
])
def test_existing_avd_with_drifted_config_fails_closed(avd_files, monkeypatch, edit, needle):
    top, cfg = avd_files
    which, key, value = edit
    ini = cfg if which == "config" else top
    lines = [l for l in ini.read_text().splitlines() if not l.startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    ini.write_text("\n".join(lines) + "\n")
    Device(monkeypatch)
    with pytest.raises(DeviceError, match=needle.replace("(", r"\(")):
        manager.create_avd()


# --- running image ----------------------------------------------------------------


def test_running_image_matches(avd_files, monkeypatch):
    Device(monkeypatch)
    manager.check_running_image()  # no raise


@pytest.mark.parametrize("kw,needle", [
    ({"avd": "labs_agent_api36"}, "running AVD 'labs_agent_api36'"),
    ({"sdk": "35"}, "ro.build.version.sdk='35'"),
    ({"abi": "x86_64"}, "ro.product.cpu.abi='x86_64'"),
])
def test_running_image_drift_fails_closed(avd_files, monkeypatch, kw, needle):
    Device(monkeypatch, **kw)
    with pytest.raises(DeviceError, match=needle):
        manager.check_running_image()


def test_running_avd_name_requires_a_reply(monkeypatch):
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "OK\r\n")
    with pytest.raises(DeviceError, match="no name"):
        manager.running_avd_name()
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "p2_KOTLIN_api36\r\n")  # no OK echoed, KO inside the name
    assert manager.running_avd_name() == "p2_KOTLIN_api36"


# --- snapshots --------------------------------------------------------------------


def test_emu_requires_ok_reply(monkeypatch):
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "")
    with pytest.raises(DeviceError, match="no reply"):
        manager._emu("avd", "snapshot", "load", "baseline")
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "KO: nope\r\n")
    with pytest.raises(DeviceError, match="KO: nope"):
        manager._emu("avd", "snapshot", "load", "baseline")
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "BOOK\r\n")  # OK inside a word is not a status
    with pytest.raises(DeviceError):
        manager._emu("avd", "snapshot", "load", "baseline")
    monkeypatch.setattr(manager, "adb", lambda *a, **kw: "OK: killing emulator, bye bye\r\nOK\r\n")
    assert "bye" in manager._emu("kill")


def test_snapshot_names_parses_loadable_rows_only(monkeypatch):
    d = Device(monkeypatch, snapshots=("baseline", "business"))
    assert manager.snapshot_names() == ["baseline", "business"]  # not "of", not the partial "broken"
    d.snapshots = []
    assert manager.snapshot_names() == []


def test_restore_checks_existence_before_loading(monkeypatch):
    d = Device(monkeypatch, snapshots=())
    with pytest.raises(DeviceError, match="does not exist") as info:
        manager.restore_snapshot("baseline")
    assert info.value.retryable is False
    assert ("avd", "snapshot", "load", "baseline") not in d.emu_calls
    d.snapshots = ["baseline"]
    assert manager.restore_snapshot("baseline") >= 0
    assert d.emu_calls[-1] == ("avd", "snapshot", "load", "baseline")


# --- baseline.json vs device --------------------------------------------------------


def _baseline(tmp_path, **override):
    info = {"avd": manager.AVD_NAME, "serial": manager.SERIAL, "snapshot": "baseline",
            "system_image": manager.SYSTEM_IMAGE, "host_loopback_blocked": True,
            "markor": {"package": manager.MARKOR_PACKAGE, "sha256": manager.MARKOR_SHA256}}
    info.update(override)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(info))
    return path


def test_baseline_matches_device(tmp_path, monkeypatch):
    Device(monkeypatch)
    out = manager.check_baseline_matches(_baseline(tmp_path))
    assert out == {"avd": manager.AVD_NAME, "serial": manager.SERIAL, "system_image": manager.SYSTEM_IMAGE,
                   "snapshot": "baseline", "markor_sha256": manager.MARKOR_SHA256}


@pytest.mark.parametrize("override,device,needle", [
    ({"avd": "labs_agent_api36"}, {}, "baseline.json avd="),
    ({"serial": "emulator-5554"}, {}, "baseline.json serial="),
    ({"system_image": "system-images;android-35;google_apis;arm64-v8a"}, {}, "system_image="),
    ({}, {"avd": "other"}, "running AVD 'other'"),
    ({}, {"snapshots": ()}, "snapshot 'baseline' missing"),
    ({}, {"apk": ""}, "is not installed"),
    ({}, {"sha": "0" * 64}, "sha256 '0000"),
    ({"markor": {"sha256": "f" * 64}}, {}, "baseline.json markor.sha256"),
    ({"snapshot": "business"}, {}, "baseline.json snapshot='business'"),
])
def test_baseline_drift_fails_closed(tmp_path, monkeypatch, override, device, needle):
    Device(monkeypatch, **device)
    with pytest.raises(DeviceError, match=needle):
        manager.check_baseline_matches(_baseline(tmp_path, **override))


SAMPLE = {"package": "com.labs.snackorders", "sha256": SAMPLE_SHA}


def test_sample_app_package_matches_the_seed_adapter():
    from harness.seed.sample_app import PACKAGE
    assert manager.SAMPLE_APP_PACKAGE == PACKAGE


def test_baseline_with_sample_app_checks_its_installed_hash(tmp_path, monkeypatch):
    dev = Device(monkeypatch)
    out = manager.check_baseline_matches(_baseline(tmp_path, sample_app=SAMPLE))
    assert out["sample_app_sha256"] == SAMPLE_SHA and out["markor_sha256"] == manager.MARKOR_SHA256
    # The hash came from the device, not from baseline.json.
    assert ("pm", "path", manager.SAMPLE_APP_PACKAGE) in dev.shell_calls
    assert ("sha256sum", dev.sample_apk) in dev.shell_calls


def test_sample_app_package_is_read_from_baseline_when_recorded(tmp_path, monkeypatch):
    dev = Device(monkeypatch, sample_package="com.example.renamed")
    out = manager.check_baseline_matches(
        _baseline(tmp_path, sample_app={"package": "com.example.renamed", "sha256": SAMPLE_SHA}))
    assert out["sample_app_sha256"] == SAMPLE_SHA
    assert ("pm", "path", "com.example.renamed") in dev.shell_calls
    assert ("pm", "path", manager.SAMPLE_APP_PACKAGE) not in dev.shell_calls


def test_baseline_without_sample_app_never_queries_it(tmp_path, monkeypatch):
    dev = Device(monkeypatch)
    assert "sample_app_sha256" not in manager.check_baseline_matches(_baseline(tmp_path))
    assert not [c for c in dev.shell_calls if manager.SAMPLE_APP_PACKAGE in c or dev.sample_apk in c]


@pytest.mark.parametrize("sample,device,needle", [
    (SAMPLE, {"sample_apk": ""}, "com.labs.snackorders is not installed"),
    (SAMPLE, {"sample_sha": "0" * 64}, "com.labs.snackorders sha256 '0000"),
    ({"package": "com.labs.snackorders"}, {}, "sample_app has no sha256"),
    ({"package": "com.labs.snackorders", "sha256": ""}, {}, "sample_app has no sha256"),
    ({"package": "com.labs.snackorders", "sha256": 123}, {}, "sample_app has no sha256"),
    (None, {}, "sample_app has no sha256"),
])
def test_sample_app_drift_fails_closed(tmp_path, monkeypatch, sample, device, needle):
    Device(monkeypatch, **device)
    with pytest.raises(DeviceError, match=needle):
        manager.check_baseline_matches(_baseline(tmp_path, sample_app=sample))


def test_sample_app_and_markor_problems_are_reported_together(tmp_path, monkeypatch):
    Device(monkeypatch, sha="1" * 64, sample_sha="2" * 64)
    with pytest.raises(DeviceError) as exc:
        manager.check_baseline_matches(_baseline(tmp_path, sample_app=SAMPLE))
    assert "net.gsantner.markor sha256 '1111" in str(exc.value)
    assert "com.labs.snackorders sha256 '2222" in str(exc.value)


def test_missing_baseline_json_fails_closed(tmp_path, monkeypatch):
    Device(monkeypatch)
    with pytest.raises(DeviceError, match="cannot read"):
        manager.check_baseline_matches(tmp_path / "nope.json")


def test_avd_listed_but_ini_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "TOP_INI", tmp_path / "missing.ini")
    monkeypatch.setattr(manager, "CONFIG_INI", tmp_path / "missing.avd" / "config.ini")
    Device(monkeypatch)
    with pytest.raises(DeviceError, match="does not exist"):
        manager.create_avd()


def test_start_kills_emulator_when_image_check_fails(avd_files, monkeypatch, tmp_path):
    class Proc:
        returncode = None
        killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    proc = Proc()
    monkeypatch.setattr(manager, "_online", lambda serial=manager.SERIAL: False)
    monkeypatch.setattr(manager.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(manager.tempfile, "gettempdir", lambda: str(tmp_path))
    Device(monkeypatch, avd="wrong_avd")
    with pytest.raises(DeviceError, match="running AVD 'wrong_avd'"):
        manager.start()
    assert proc.killed
