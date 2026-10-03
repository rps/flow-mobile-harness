"""LABS_AVD_NAME / LABS_AVD_PORT handling in harness.emulator.manager."""

import importlib
from pathlib import Path

import pytest

from harness.contracts import DeviceError
from harness.emulator import manager


class _Snapshot:
    def __init__(self, m):
        self.AVD_NAME, self.PORT, self.SERIAL, self.TOP_INI, self.CONFIG_INI = (
            m.AVD_NAME, m.PORT, m.SERIAL, m.TOP_INI, m.CONFIG_INI)
        self.SYSTEM_IMAGE, self.SYSTEM_IMAGE_DIR = m.SYSTEM_IMAGE, m.SYSTEM_IMAGE_DIR


def _reload(monkeypatch, **env):
    """Reload manager with the given env and return a snapshot of its
    derived values; the module is reloaded with the real env afterwards."""
    for k in ("LABS_AVD_NAME", "LABS_AVD_PORT", "LABS_SYSTEM_IMAGE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        return _Snapshot(importlib.reload(manager))
    finally:
        monkeypatch.undo()
        importlib.reload(manager)


def test_defaults_and_derived_values(monkeypatch):
    m = _reload(monkeypatch)
    assert (m.AVD_NAME, m.PORT, m.SERIAL) == ("p2_harness_api36", 5584, "emulator-5584")
    assert m.SYSTEM_IMAGE == "system-images;android-36.1;google_apis;arm64-v8a"
    assert m.SYSTEM_IMAGE_DIR.parts == ("system-images", "android-36.1", "google_apis", "arm64-v8a")
    assert m.TOP_INI.name == "p2_harness_api36.ini" and m.CONFIG_INI.parent.name == "p2_harness_api36.avd"


def test_env_overrides_follow_through(monkeypatch):
    m = _reload(monkeypatch, LABS_AVD_NAME="p2_other", LABS_AVD_PORT="5592")
    assert (m.AVD_NAME, m.PORT, m.SERIAL) == ("p2_other", 5592, "emulator-5592")
    assert m.TOP_INI.name == "p2_other.ini"


@pytest.mark.parametrize("bad", ["abc", "5585", "5552", "5684", ""])
def test_invalid_port_is_a_clear_error(monkeypatch, bad):
    with pytest.raises(DeviceError, match="LABS_AVD_PORT"):
        _reload(monkeypatch, LABS_AVD_PORT=bad)


def test_system_image_override_drives_create_and_source_properties(monkeypatch, tmp_path):
    m = _reload(monkeypatch, LABS_SYSTEM_IMAGE="system-images;android-36;google_apis;x86_64")
    assert m.SYSTEM_IMAGE_DIR == Path("system-images/android-36/google_apis/x86_64")
    # _image_api_level reads source.properties under that directory
    monkeypatch.setenv("LABS_SYSTEM_IMAGE", "system-images;android-36;google_apis;x86_64")
    importlib.reload(manager)
    try:
        props = tmp_path / manager.SYSTEM_IMAGE_DIR / "source.properties"
        props.parent.mkdir(parents=True)
        props.write_text("Pkg.Desc=x\nAndroidVersion.ApiLevel=36\n")
        monkeypatch.setattr(manager, "SDK", tmp_path)
        assert manager._image_api_level() == "36"
        calls = []
        monkeypatch.setattr(manager, "_run", lambda cmd, timeout=120.0, stdin=None: calls.append(cmd) or "")
        monkeypatch.setattr(manager, "_set_ini_keys", lambda ini, values: calls.append((ini.name, values)))
        assert manager.create_avd() is True
        assert "system-images;android-36;google_apis;x86_64" in calls[1]
        assert calls[2] == (f"{manager.AVD_NAME}.ini", {"target": "android-36"})
    finally:
        monkeypatch.undo()
        importlib.reload(manager)


@pytest.mark.parametrize("bad", ["android-36;google_apis;x86_64", "system-images;android-36;x86_64",
                                 "system-images;;google_apis;x86_64", "system-images;a;b;c;d"])
def test_invalid_system_image_is_a_clear_error(monkeypatch, bad):
    with pytest.raises(DeviceError, match="LABS_SYSTEM_IMAGE"):
        _reload(monkeypatch, LABS_SYSTEM_IMAGE=bad)
