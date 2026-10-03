"""The business profile fails closed: no freeform, no run on a missing or stale baseline."""

import json

import pytest

from harness.emulator.profiles import business
from harness.emulator.profiles.business import ProfileError


def _good() -> dict:
    return {"avd": business.AVD_NAME, "serial": business.SERIAL, "snapshot": business.SNAPSHOT,
            "freeform_allowed": False, "host_loopback_blocked": False, "root": False}


def test_committed_baseline_is_consistent_and_says_unblocked():
    info = business.baseline_info()
    assert info["root"] is False and info["host_loopback_blocked"] is False and info["freeform_allowed"] is False
    assert info["system_image"] == business.SYSTEM_IMAGE


def test_missing_baseline_refuses(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        business.baseline_info(tmp_path / "nope.json")


def test_unreadable_baseline_refuses(tmp_path):
    bad = tmp_path / "business.json"
    bad.write_text("{not json")
    with pytest.raises(ProfileError, match="cannot read"):
        business.baseline_info(bad)
    bad.write_text("[]")
    with pytest.raises(ProfileError, match="not a JSON object"):
        business.baseline_info(bad)


@pytest.mark.parametrize("key, value", [
    ("avd", "p2_harness_api36"), ("serial", "emulator-5584"), ("snapshot", "baseline"),
    ("freeform_allowed", True), ("freeform_allowed", None), ("host_loopback_blocked", True), ("host_loopback_blocked", None),
])
def test_stale_or_permissive_baseline_refuses(tmp_path, key, value):
    info = _good()
    if value is None:
        info.pop(key)
    else:
        info[key] = value
    path = tmp_path / "business.json"
    path.write_text(json.dumps(info))
    with pytest.raises(ProfileError, match=key):
        business.baseline_info(path)


def test_assert_scored_refuses_freeform_and_empty_task(tmp_path):
    path = tmp_path / "business.json"
    path.write_text(json.dumps(_good()))
    business.assert_scored("biz_b1_hours", None, path)
    with pytest.raises(ProfileError, match="freeform"):
        business.assert_scored(None, "open chrome", path)
    with pytest.raises(ProfileError, match="freeform"):
        business.assert_scored("biz_b1_hours", "and also this", path)
    with pytest.raises(ProfileError, match="freeform"):
        business.assert_scored("", None, path)


def test_run_scored_never_passes_a_goal_and_pins_baseline(monkeypatch):
    seen = {}

    def fake_run_task(task_id, goal, config, policy, **kw):
        seen.update(task_id=task_id, goal=goal, **kw)
        return "record"

    class FakeEnv:
        device_factory = inspector_factory = emulator = object()

    monkeypatch.setattr("harness.runner.run_task", fake_run_task)
    monkeypatch.setattr(business, "env", lambda config, windowed=True: FakeEnv())
    out = business.run_scored("biz_h", object(), "approve", store="store", goal="sneaky", meta={"k": 1})
    assert out == "record"
    assert seen["goal"] is None and seen["baseline_json"] == business.BASELINE_JSON
    assert seen["meta"] == {"k": 1, "profile": "business"}


def test_restore_snapshot_rejects_other_names(monkeypatch):
    calls = []
    monkeypatch.setattr(business, "manager", lambda: type("M", (), {"restore_snapshot": staticmethod(
        lambda name, timeout=0: calls.append(name) or 1.0)})())
    business.restore_snapshot("baseline")
    business.restore_snapshot()
    assert calls == [business.SNAPSHOT, business.SNAPSHOT]
    with pytest.raises(ProfileError, match="only snapshot"):
        business.restore_snapshot("probe")


def test_manager_binding_refuses_a_manager_bound_elsewhere(monkeypatch):
    import sys
    import types

    other = types.ModuleType("harness.emulator.manager")
    other.AVD_NAME, other.PORT = "p2_harness_api36", 5584
    monkeypatch.setitem(sys.modules, "harness.emulator.manager", other)
    with pytest.raises(ProfileError, match="already bound"):
        business.manager()


def test_env_prefix_names_every_override():
    assert business.ENV_PREFIX == ("LABS_AVD_NAME=p2_business_play LABS_AVD_PORT=5592 "
                                   "LABS_SYSTEM_IMAGE='system-images;android-36.1;google_apis_playstore;arm64-v8a'")
    import shlex
    assert dict(kv.split("=", 1) for kv in shlex.split(business.ENV_PREFIX)) == business.ENV
