import hashlib
import json
from datetime import datetime, timezone

import pytest

from harness.contracts import DeviceError
from harness.emulator import manager
from harness.emulator import provision_sample_app as prov
from harness.emulator.provision_sample_app import BaselineKeyConflict, add_baseline_keys, sha256_of


def test_add_baseline_keys_adds_keeps_equal_and_refuses_conflicts(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"avd": "x", "markor": {"v": 1}}))
    out = add_baseline_keys(path, {"sample_app": {"sha256": "abc"}})
    assert out == {"avd": "x", "markor": {"v": 1}, "sample_app": {"sha256": "abc"}}
    assert json.loads(path.read_text()) == out
    add_baseline_keys(path, {"sample_app": {"sha256": "abc"}})  # same value: fine
    with pytest.raises(BaselineKeyConflict):
        add_baseline_keys(path, {"sample_app": {"sha256": "def"}})
    with pytest.raises(BaselineKeyConflict):
        add_baseline_keys(path, {"avd": "y"})
    assert json.loads(path.read_text())["avd"] == "x"  # untouched after the refusal
    missing = tmp_path / "new.json"
    assert add_baseline_keys(missing, {"a": 1}) == {"a": 1}
    bad = tmp_path / "list.json"
    bad.write_text("[]")
    with pytest.raises(BaselineKeyConflict):
        add_baseline_keys(bad, {"a": 1})


def test_sha256_of(tmp_path):
    f = tmp_path / "x.apk"
    f.write_bytes(b"apk bytes")
    assert sha256_of(f) == hashlib.sha256(b"apk bytes").hexdigest()


class FakeManager:
    def __init__(self, online=True, pkg_listed=True, provider_ok=True):
        self.calls = []
        self.online = online
        self.pkg_listed = pkg_listed
        self.provider_ok = provider_ok

    def shell(self, serial, *args, timeout=30.0, binary=False):
        self.calls.append(("shell", args[1]))
        cmd = args[1]
        if cmd == "pm list packages":
            return "package:com.android.settings\n" + (f"package:{prov.PACKAGE}\n" if self.pkg_listed else "")
        if cmd.startswith("dumpsys package"):
            return self.dumpsys
        if cmd.startswith("content query"):
            return "Row: 0 _id=1001\n" if self.provider_ok else self.provider_error
        return ""

    dumpsys = "  Packages:\n    versionName=1.0-labs\n"
    provider_error = "Error while accessing provider\n"


@pytest.fixture
def fake_manager(monkeypatch, tmp_path):
    fm = FakeManager()
    monkeypatch.setattr(manager, "_online", lambda: fm.online)
    monkeypatch.setattr(manager, "start", lambda windowed=False: fm.calls.append(("start", windowed)) or 12.5)
    monkeypatch.setattr(manager, "restore_snapshot", lambda name: fm.calls.append(("restore", name)) or 1.5)
    monkeypatch.setattr(manager, "install_apk", lambda path, sha=None: fm.calls.append(("install", str(path), sha)))
    monkeypatch.setattr(manager, "save_snapshot", lambda name: fm.calls.append(("save", name)))
    monkeypatch.setattr(manager, "_shell", lambda *a, timeout=30.0: fm.shell(None, "shell", " ".join(a), timeout=timeout))
    monkeypatch.setattr(prov.time, "sleep", lambda s: None)
    apk = tmp_path / "snackorders-debug.apk"
    apk.write_bytes(b"fake apk")
    fm.apk = apk
    fm.baseline = tmp_path / "baseline.json"
    fm.baseline.write_text(json.dumps({"avd": "labs", "host_loopback_blocked": True}))
    return fm


def test_provision_sequence_and_baseline_record(fake_manager):
    fm = fake_manager
    info = prov.provision(fm.apk, windowed=True, baseline_json=fm.baseline)
    digest = hashlib.sha256(b"fake apk").hexdigest()
    kinds = [c[0] for c in fm.calls]
    assert kinds.index("restore") < kinds.index("install") < kinds.index("save")
    assert ("install", str(fm.apk), digest) in fm.calls
    assert ("restore", "baseline") in fm.calls and ("save", "baseline") in fm.calls
    assert ("shell", f"am force-stop {prov.PACKAGE}") in fm.calls
    assert fm.calls.index(("shell", f"am force-stop {prov.PACKAGE}")) < kinds.index("save")
    assert "start" not in kinds  # already online
    data = json.loads(fm.baseline.read_text())
    assert data["host_loopback_blocked"] is True and data["avd"] == "labs"  # existing keys kept
    assert data["sample_app"]["sha256"] == digest and data["sample_app"]["package"] == prov.PACKAGE
    assert data["sample_app"]["version_name"] == "1.0-labs"
    assert data["sample_app"]["package_count_after_install"] == 2
    assert info["sha256"] == digest and info["boot_s"] is None and info["restore_s"] == 1.5


def test_provision_boots_when_offline_and_passes_windowed(fake_manager):
    fm = fake_manager
    fm.online = False
    info = prov.provision(fm.apk, windowed=True, baseline_json=fm.baseline)
    assert ("start", True) in fm.calls and info["boot_s"] == 12.5


def test_provision_refuses_missing_apk_and_failed_install(fake_manager, tmp_path):
    fm = fake_manager
    with pytest.raises(DeviceError, match="APK not found"):
        prov.provision(tmp_path / "nope.apk", baseline_json=fm.baseline)
    assert fm.calls == []  # the emulator was never touched
    fm.pkg_listed = False
    with pytest.raises(DeviceError, match="not listed"):
        prov.provision(fm.apk, baseline_json=fm.baseline)
    assert ("save", "baseline") not in fm.calls  # no re-baseline on failure
    assert "sample_app" not in json.loads(fm.baseline.read_text())


@pytest.mark.parametrize("error", ["Error while accessing provider\n", "java.lang.SecurityException: denied\n", ""])
def test_provision_refuses_when_provider_does_not_answer(fake_manager, error):
    fm = fake_manager
    fm.provider_ok = False
    fm.provider_error = error
    with pytest.raises(DeviceError, match="provider"):
        prov.provision(fm.apk, baseline_json=fm.baseline)
    assert ("save", "baseline") not in fm.calls


def test_provision_accepts_an_empty_order_list_from_the_provider(fake_manager):
    fm = fake_manager
    fm.provider_ok = False
    fm.provider_error = "No result found.\n"  # a fresh install has no orders
    prov.provision(fm.apk, baseline_json=fm.baseline)
    assert ("save", "baseline") in fm.calls


def test_provision_failed_install_saves_nothing(fake_manager, monkeypatch):
    fm = fake_manager

    def boom(path, sha=None):
        fm.calls.append(("install", str(path), sha))
        raise DeviceError("install failed")

    monkeypatch.setattr(manager, "install_apk", boom)
    with pytest.raises(DeviceError, match="install failed"):
        prov.provision(fm.apk, baseline_json=fm.baseline)
    assert ("save", "baseline") not in fm.calls
    assert "sample_app" not in json.loads(fm.baseline.read_text())


def test_provision_refuses_a_recorded_entry_without_a_hash(fake_manager):
    fm = fake_manager
    fm.baseline.write_text(json.dumps({"sample_app": {}}))
    with pytest.raises(BaselineKeyConflict):
        prov.provision(fm.apk, baseline_json=fm.baseline)
    assert fm.calls == []


def test_provision_records_empty_version_name_when_dumpsys_has_none(fake_manager):
    fm = fake_manager
    fm.dumpsys = "  Packages:\n"
    info = prov.provision(fm.apk, baseline_json=fm.baseline)
    assert info["version_name"] == "" and json.loads(fm.baseline.read_text())["sample_app"]["version_name"] == ""


def test_provision_refuses_a_different_recorded_apk_before_touching_the_emulator(fake_manager):
    fm = fake_manager
    fm.baseline.write_text(json.dumps({"sample_app": {"sha256": "old"}}))
    with pytest.raises(BaselineKeyConflict):
        prov.provision(fm.apk, baseline_json=fm.baseline)
    assert fm.calls == []
    assert json.loads(fm.baseline.read_text()) == {"sample_app": {"sha256": "old"}}


def test_provision_twice_with_the_same_apk_is_idempotent(fake_manager):
    fm = fake_manager
    first = prov.provision(fm.apk, baseline_json=fm.baseline)
    recorded = json.loads(fm.baseline.read_text())["sample_app"]
    fm.calls.clear()
    second = prov.provision(fm.apk, baseline_json=fm.baseline)
    kinds = [c[0] for c in fm.calls]
    assert kinds.index("restore") < kinds.index("install") < kinds.index("save")  # re-provisioned
    assert json.loads(fm.baseline.read_text())["sample_app"] == recorded  # record kept, installed_at unchanged
    assert first["installed_at"] == second["installed_at"] == recorded["installed_at"]


def _installs(fm):
    return [c for c in fm.calls if c[0] == "install"]


@pytest.mark.parametrize("old", [{"sha256": "old", "installed_at": "2026-10-03T02:17:02+00:00"}, {"sha256": "old"}])
def test_upgrade_installs_the_new_apk_over_a_different_record_and_replaces_only_that_entry(fake_manager, old):
    fm = fake_manager
    fm.baseline.write_text(json.dumps({"avd": "labs", "host_loopback_blocked": True, "sample_app": old}))
    before = datetime.now(timezone.utc).replace(microsecond=0)
    info = prov.provision(fm.apk, baseline_json=fm.baseline, upgrade=True)
    digest = hashlib.sha256(b"fake apk").hexdigest()
    assert _installs(fm) == [("install", str(fm.apk), digest)]  # one install of the new APK (install -r)
    kinds = [c[0] for c in fm.calls]
    assert kinds.index("restore") < kinds.index("install") < kinds.index("save")
    data = json.loads(fm.baseline.read_text())
    assert {k: data[k] for k in ("avd", "host_loopback_blocked")} == {"avd": "labs", "host_loopback_blocked": True}
    entry = data["sample_app"]
    assert entry["sha256"] == digest == info["sha256"] and entry["replaces_sha256"] == "old"
    assert before <= datetime.fromisoformat(entry["installed_at"]) <= datetime.now(timezone.utc)


def test_upgrade_with_the_same_apk_reinstalls_and_keeps_the_record(fake_manager):
    fm = fake_manager
    prov.provision(fm.apk, baseline_json=fm.baseline)
    recorded = json.loads(fm.baseline.read_text())["sample_app"]
    fm.calls.clear()
    prov.provision(fm.apk, baseline_json=fm.baseline, upgrade=True)
    assert len(_installs(fm)) == 1 and ("save", "baseline") in fm.calls
    assert json.loads(fm.baseline.read_text())["sample_app"] == recorded  # nothing replaced, no replaces_sha256


def test_upgrade_without_a_recorded_entry_adds_one_like_a_first_install(fake_manager):
    fm = fake_manager
    prov.provision(fm.apk, baseline_json=fm.baseline, upgrade=True)
    entry = json.loads(fm.baseline.read_text())["sample_app"]
    assert entry["sha256"] == hashlib.sha256(b"fake apk").hexdigest() and "replaces_sha256" not in entry


def _boom_install(fm):
    def boom(path, sha=None):
        fm.calls.append(("install", str(path), sha))
        raise DeviceError("install failed")
    return boom


@pytest.mark.parametrize("failure", ["provider", "install", "not_listed"])
def test_failed_upgrade_saves_nothing_and_keeps_the_old_record(fake_manager, monkeypatch, failure):
    fm = fake_manager
    fm.baseline.write_text(json.dumps({"sample_app": {"sha256": "old"}}))
    if failure == "provider":
        fm.provider_ok = False
    elif failure == "install":
        monkeypatch.setattr(manager, "install_apk", _boom_install(fm))
    else:
        fm.pkg_listed = False
    with pytest.raises(DeviceError):
        prov.provision(fm.apk, baseline_json=fm.baseline, upgrade=True)
    assert len(_installs(fm)) == 1 and ("save", "baseline") not in fm.calls
    assert json.loads(fm.baseline.read_text()) == {"sample_app": {"sha256": "old"}}


def test_cli_passes_baseline_json_and_upgrade(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(prov, "provision", lambda apk, **kw: seen.update(apk=apk, **kw) or {})
    prov.main(["--apk", "x.apk", "--baseline-json", str(tmp_path / "b.json"), "--upgrade"])
    assert (seen["baseline_json"], seen["upgrade"]) == (str(tmp_path / "b.json"), True)
    prov.main([])
    assert (seen["baseline_json"], seen["upgrade"]) == (None, False)
