"""The business profile fails closed: no freeform, no run on a missing or stale baseline."""

import json

import pytest

from harness.emulator.profiles import business
from harness.emulator.profiles.business import ProfileError


@pytest.fixture(autouse=True)
def _mac_profile(monkeypatch):
    """These tests describe the committed (Mac) profile. A shell that exports
    LABS_BUSINESS_* (the cloud VM's $BIZ) rebinds the module at import, so pin
    the defaults in-process; the override tests use a fresh interpreter."""
    image = "system-images;android-36.1;google_apis_playstore;arm64-v8a"
    for name, value in (("AVD_NAME", "p2_business_play"), ("PORT", 5592), ("SERIAL", "emulator-5592"),
                        ("SYSTEM_IMAGE", image), ("BASELINE_JSON", business.HERE / "business.json"),
                        ("ENV", {"LABS_AVD_NAME": "p2_business_play", "LABS_AVD_PORT": "5592", "LABS_SYSTEM_IMAGE": image})):
        monkeypatch.setattr(business, name, value)


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
    monkeypatch.setattr(business, "env", lambda config, windowed=True, invoice_api=None: FakeEnv())
    out = business.run_scored("biz_h", object(), "approve", store="store", goal="sneaky", meta={"k": 1})
    assert out == "record"
    assert seen["goal"] is None and seen["baseline_json"] == business.BASELINE_JSON
    assert seen["meta"] == {"k": 1, "profile": "business", "oracle": business.oracle_meta(None)}
    assert seen["meta"]["oracle"]["invoices"] == "invoice_ninja_screen_readback" and seen["meta"]["oracle"]["invoices_tier"] == 5
    assert tuple(seen["block_packages"]) == ("com.android.vending",)


def test_run_scored_keeps_caller_block_packages_alongside_play_store(monkeypatch):
    seen = {}

    class FakeEnv:
        device_factory = inspector_factory = emulator = object()

    monkeypatch.setattr("harness.runner.run_task", lambda task_id, goal, config, policy, **kw: seen.update(kw))
    monkeypatch.setattr(business, "env", lambda config, windowed=True, invoice_api=None: FakeEnv())
    business.run_scored("biz_h", object(), "approve", store="store", block_packages=["org.example.x"])
    assert sorted(seen["block_packages"]) == ["com.android.vending", "org.example.x"]


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
    import shlex

    prefix, env = _import_profile_attrs(["ENV_PREFIX", "ENV"], {})  # Mac defaults, whatever the shell exports
    assert prefix == ("LABS_AVD_NAME=p2_business_play LABS_AVD_PORT=5592 "
                      "LABS_SYSTEM_IMAGE='system-images;android-36.1;google_apis_playstore;arm64-v8a'")
    assert dict(kv.split("=", 1) for kv in shlex.split(prefix)) == env


# --- Invoice oracle and the biz_d cleanup -----------------------------------------

from harness.contracts import OracleTier  # noqa: E402
from harness.verify import biz_api  # noqa: E402
from tests.biz_fakes import API_ROOT, FakeInvoiceServer  # noqa: E402


def _server():
    return FakeInvoiceServer(
        [{"id": "nw", "name": "Northwind Traders"}, {"id": "zz", "name": "ZZ Probe Test Client"}],
        [{"id": "a", "number": "0001_Deleted", "client_id": "nw", "amount": 403.75, "status_id": "1",
          "is_deleted": True, "archived_at": 1, "line_items": []},
         {"id": "b", "number": "0002", "client_id": "nw", "amount": 403.75, "status_id": "1",
          "is_deleted": False, "archived_at": None, "line_items": []},
         {"id": "c", "number": "0003", "client_id": "zz", "amount": 120.0, "status_id": "2",
          "is_deleted": False, "archived_at": None, "line_items": []}])


class _Captured:
    """Stands in for runner.run_task and records what run_scored passed."""

    def __init__(self):
        self.kw = {}

    def __call__(self, task_id, goal, config, policy, **kw):
        self.kw = dict(kw, task_id=task_id)
        return "record"


def _with_api(monkeypatch, server, tier_of=("biz_d_invoice", "biz_h")):
    """API configured: from_env returns a client on the fake server, the
    environment says tier 2 and the invoice tasks declare it."""
    from harness.tasks import registry

    api = biz_api.InvoiceNinjaApi(API_ROOT, "tok", opener=server)
    monkeypatch.setattr(biz_api, "from_env", lambda environ=None: api)
    monkeypatch.setenv(biz_api.ENV_KEY, "tok")
    monkeypatch.setenv(biz_api.ENV_ENDPOINT, API_ROOT)
    for tid in tier_of:
        monkeypatch.setattr(registry.get(tid), "oracle_tier", OracleTier.APP_EXPORT_API)
    return api


def _fake_env(monkeypatch, built):
    class FakeEnv:
        device_factory = emulator = object()

        @staticmethod
        def inspector_factory():
            built.append("inspector")
            return "inspector"

    def env(config, windowed=True, invoice_api=None):
        built.append(("env", invoice_api))
        return FakeEnv()

    monkeypatch.setattr(business, "env", env)


def test_run_scored_with_the_api_cleans_up_only_when_the_runner_builds_the_inspector(monkeypatch):
    server, built, cap = _server(), [], _Captured()
    api = _with_api(monkeypatch, server)
    _fake_env(monkeypatch, built)
    monkeypatch.setattr("harness.runner.run_task", cap)
    business.run_scored("biz_d_invoice", object(), "approve", store="s")
    meta = cap.kw["meta"]
    assert meta["oracle"]["invoices"] == "invoice_ninja_api" and meta["oracle"]["invoices_tier"] == 2
    assert meta["oracle"]["hours_range"] == "2026-09-28..2026-10-03"
    assert built == [("env", api)]  # the inspector is paired with the same API client
    assert meta["invoice_cleanup"] == {"status": "pending"}
    assert "com.android.vending" in cap.kw["block_packages"]  # Play Store block kept alongside the cleanup
    assert not [r for r in server.requests if r[0] == "DELETE"]  # nothing deleted before the runner asks
    assert cap.kw["inspector_factory"]() == "inspector"
    assert [p for m, p, _, _ in server.requests if m == "DELETE"] == ["/api/v1/invoices/b"]
    assert meta["invoice_cleanup"]["status"] == "done"
    assert [r["number"] for r in meta["invoice_cleanup"]["removed"]] == ["0002"]
    assert next(r for r in server.invoices if r["id"] == "c")["number"] == "0003"  # other client untouched


def test_run_scored_cleanup_failure_is_recorded_and_stops_the_inspector(monkeypatch):
    server, built, cap = _server(), [], _Captured()
    server.fail["/api/v1/invoices/b"] = 500
    _with_api(monkeypatch, server)
    _fake_env(monkeypatch, built)
    monkeypatch.setattr("harness.runner.run_task", cap)
    business.run_scored("biz_d_invoice", object(), "approve", store="s")
    with pytest.raises(biz_api.InvoiceNinjaError, match="HTTP 500"):
        cap.kw["inspector_factory"]()
    assert cap.kw["meta"]["invoice_cleanup"]["status"].startswith("failed: InvoiceNinjaError")
    assert "inspector" not in built


def test_run_scored_without_the_api_skips_cleanup_and_biz_h_never_cleans(monkeypatch):
    built, cap = [], _Captured()
    monkeypatch.delenv(biz_api.ENV_KEY, raising=False)
    monkeypatch.delenv(biz_api.ENV_ENDPOINT, raising=False)
    _fake_env(monkeypatch, built)
    monkeypatch.setattr("harness.runner.run_task", cap)
    business.run_scored("biz_d_invoice", object(), "approve", store="s")
    assert cap.kw["meta"]["invoice_cleanup"] == {"status": "skipped: invoice API not configured"}
    cap.kw["inspector_factory"]()
    assert cap.kw["meta"]["invoice_cleanup"] == {"status": "skipped: invoice API not configured"}

    server = _server()
    _with_api(monkeypatch, server)
    business.run_scored("biz_h", object(), "approve", store="s")
    cap.kw["inspector_factory"]()
    assert "invoice_cleanup" not in cap.kw["meta"] and not [r for r in server.requests if r[0] == "DELETE"]


def test_run_scored_refuses_tier_5_task_when_the_environment_selects_the_api(monkeypatch):
    built, cap = [], _Captured()
    _fake_env(monkeypatch, built)
    monkeypatch.setattr("harness.runner.run_task", cap)
    server = _server()
    _with_api(monkeypatch, server, tier_of=())  # API configured, tasks still declare tier 5
    with pytest.raises(ProfileError, match="declares oracle tier 5 but the environment selects tier 2"):
        business.run_scored("biz_h", object(), "approve", store="s")
    assert cap.kw == {} and built == [] and server.requests == []


def test_run_scored_refuses_tier_2_task_when_the_api_is_not_configured(monkeypatch):
    from harness.tasks import registry

    built, cap = [], _Captured()
    _fake_env(monkeypatch, built)
    monkeypatch.setattr("harness.runner.run_task", cap)
    monkeypatch.delenv(biz_api.ENV_KEY, raising=False)
    monkeypatch.setenv(biz_api.ENV_ENDPOINT, API_ROOT)
    monkeypatch.setattr(registry.get("biz_d_invoice"), "oracle_tier", OracleTier.APP_EXPORT_API)
    with pytest.raises(ProfileError, match="declares oracle tier 2 but the environment selects tier 5"):
        business.run_scored("biz_d_invoice", object(), "approve", store="s")
    assert cap.kw == {} and built == []


def test_cleanup_reaches_run_json_through_the_real_runner(monkeypatch, tmp_path):
    """run_task copies meta shallowly; the cleanup record placed in it by
    run_scored must be the dict that run.json ends up holding."""
    from harness.contracts import Config
    from harness.fake_env import FAKE_MODEL, ScriptedClient
    from harness.agent.loop import AgentSettings
    from harness.trace.store import TraceStore
    from tests.agent_fakes import FakeDevice
    from tests.biz_fakes import FakeBizInspector

    server = _server()
    _with_api(monkeypatch, server)
    order = []

    class Emu:
        SERIAL = "test-biz"

        def restore_snapshot(self, name):
            order.append("restore")

    class Insp(FakeBizInspector):
        def snapshot_state(self):
            order.append("snapshot")
            return super().snapshot_state()

    class FakeEnv:
        emulator = Emu()
        device_factory = FakeDevice

        @staticmethod
        def inspector_factory():
            order.append(("deleted", [p for m, p, _, _ in server.requests if m == "DELETE"]))
            return Insp()

    monkeypatch.setattr(business, "env", lambda config, windowed=True, invoice_api=None: FakeEnv())
    store = TraceStore(tmp_path / "runs")
    config = Config(model=FAKE_MODEL, runs_dir=str(store.runs_dir))
    run = business.run_scored("biz_d_invoice", config, "approve", store, model_client=ScriptedClient(),
                              settings=AgentSettings(allow_unpriced=True))
    on_disk, _ = store.load_run(run.run_id)
    assert on_disk.meta["invoice_cleanup"]["status"] == "done", on_disk.meta
    assert [r["number"] for r in on_disk.meta["invoice_cleanup"]["removed"]] == ["0002"]
    assert on_disk.meta["oracle"]["invoices"] == "invoice_ninja_api"
    assert on_disk.verifier_result is not None and on_disk.verifier_result.oracle_tier == OracleTier.APP_EXPORT_API
    assert order[:3] == ["restore", ("deleted", ["/api/v1/invoices/b"]), "snapshot"]  # after restore, before pre-state


def test_cleanup_invoices_needs_the_api_and_the_cli_prints_the_result(monkeypatch, capsys):
    monkeypatch.setattr(biz_api, "from_env", lambda environ=None: None)
    with pytest.raises(ProfileError, match="INVOICE_NINJA_API_KEY"):
        business.cleanup_invoices()
    server = _server()
    _with_api(monkeypatch, server)
    business.main(["cleanup-invoices"])
    out = json.loads(capsys.readouterr().out)
    assert out["client"] == "Northwind Traders" and [r["number_after"] for r in out["removed"]] == ["0002_Deleted"]


# --- Host overrides (the cloud VM's own business AVD) and the read-back CLI ------

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from harness.verify.biz_readback import Invoice, LineItem  # noqa: E402

_PROBE = ("import json; from harness.emulator.profiles import business as b; "
          "print(json.dumps([b.AVD_NAME, b.PORT, b.SERIAL, b.SYSTEM_IMAGE, str(b.BASELINE_JSON), b.ENV]))")


def _import_profile(env_overrides: dict, probe: str = _PROBE) -> list:
    """Import the profile in a fresh interpreter (the constants are read at import)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LABS_BUSINESS_", "LABS_AVD_", "LABS_SYSTEM_"))}
    env.update(env_overrides, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _import_profile_attrs(names: list, env_overrides: dict) -> list:
    probe = ("import json; from harness.emulator.profiles import business as b; "
             f"print(json.dumps([getattr(b, n) for n in {names!r}]))")
    return _import_profile(env_overrides, probe)


def test_profile_defaults_are_the_macs_avd_and_ignore_the_harness_avd_variables():
    name, port, serial, image, baseline, env = _import_profile(
        {"LABS_AVD_NAME": "cloud_harness", "LABS_AVD_PORT": "5584",
         "LABS_SYSTEM_IMAGE": "system-images;android-36.1;google_apis;x86_64"})
    assert (name, port, serial) == ("p2_business_play", 5592, "emulator-5592")
    assert image == "system-images;android-36.1;google_apis_playstore;arm64-v8a"
    assert baseline == str(business.HERE / "business.json")
    assert env["LABS_AVD_NAME"] == "p2_business_play"  # what manager() exports, not the shell's cloud_harness


def test_business_variables_rebind_avd_serial_image_and_record(tmp_path):
    record = tmp_path / "vm_business.json"
    name, port, serial, image, baseline, env = _import_profile(
        {"LABS_BUSINESS_AVD_NAME": "cloud_business", "LABS_BUSINESS_AVD_PORT": "5586",
         "LABS_BUSINESS_SYSTEM_IMAGE": "system-images;android-36.1;google_apis_playstore;x86_64",
         "LABS_BUSINESS_BASELINE": str(record)})
    assert (name, port, serial, baseline) == ("cloud_business", 5586, "emulator-5586", str(record))
    assert env == {"LABS_AVD_NAME": "cloud_business", "LABS_AVD_PORT": "5586",
                   "LABS_SYSTEM_IMAGE": "system-images;android-36.1;google_apis_playstore;x86_64"}


def test_business_baseline_path_expands_home_and_empty_falls_back_to_the_committed_record():
    assert _import_profile({"LABS_BUSINESS_BASELINE": "~/x/business.json"})[4] == str(Path.home() / "x/business.json")
    assert _import_profile({"LABS_BUSINESS_BASELINE": ""})[4] == str(business.HERE / "business.json")


def test_non_integer_business_port_fails_at_import():
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _import_profile({"LABS_BUSINESS_AVD_PORT": "fifty"})
    assert "invalid literal for int" in exc.value.stderr


def test_record_from_another_avd_is_refused_under_the_mac_defaults(tmp_path):
    """A VM record (cloud_business) must never validate on a host bound to p2_business_play."""
    info = dict(_good(), avd="cloud_business", serial="emulator-5586")
    path = tmp_path / "vm_business.json"
    path.write_text(json.dumps(info))
    with pytest.raises(ProfileError, match="avd='cloud_business'"):
        business.baseline_info(path)


class _FakeReader:
    def __init__(self, invoices=None):
        self.calls = []
        self._invoices = invoices or {}

    def timecamp_project_hours(self):
        self.calls.append("hours")
        return {"Northwind Traders": 4.25, "Ideation": 5.0}

    def insightly_org_description(self, org):
        self.calls.append(("org", org))
        return "Hourly rate: 95 USD per hour"

    def invoice_ninja_invoices(self, known=None):
        self.calls.append("invoices")
        return self._invoices, False


def test_readback_takes_invoices_from_the_api_when_configured_and_never_opens_the_app():
    reader = _FakeReader()
    api = biz_api.InvoiceNinjaApi(API_ROOT, "tok", opener=_server())
    out = business.readback(api=api, reader=reader)
    assert out["range"] == "2026-09-28..2026-10-03"
    assert out["hours"]["Northwind Traders"] == 4.25
    assert out["org_descriptions"] == {"Northwind Traders": "Hourly rate: 95 USD per hour"}
    assert out["invoices_oracle"] == "invoice_ninja_api" and out["invoices_complete"] is True
    assert sorted(out["invoices"]) == ["0001_Deleted", "0002", "0003"]
    assert out["invoices"]["0002"]["client"] == "Northwind Traders" and out["invoices"]["0001_Deleted"]["state"] == "Deleted"
    assert "invoices" not in reader.calls


def test_readback_without_the_api_reads_the_screen_and_reports_an_incomplete_scan(monkeypatch):
    monkeypatch.setattr(biz_api, "from_env", lambda environ=None: None)
    inv = Invoice("0004", "Northwind Traders", 403.75, "Draft", "10/03/2026", (LineItem("Consulting hours", 403.75, 4.25, 95.0),))
    reader = _FakeReader({"0004": inv})
    out = business.readback(reader=reader)
    assert out["invoices_oracle"] == "invoice_ninja_screen_readback" and out["invoices_complete"] is False
    assert out["invoices"]["0004"]["items"][0] == {"name": "Consulting hours", "total": 403.75, "quantity": 4.25, "unit_cost": 95.0}
    assert reader.calls == ["hours", ("org", "Northwind Traders"), "invoices"]


def test_readback_api_error_propagates_instead_of_falling_back_to_the_screen():
    reader = _FakeReader()
    api = biz_api.InvoiceNinjaApi(API_ROOT, "tok", opener=FakeInvoiceServer([], [], fail={"/api/v1/clients": 500}))
    with pytest.raises(biz_api.InvoiceNinjaError, match="HTTP 500"):
        business.readback(api=api, reader=reader)
    assert "invoices" not in reader.calls


def test_readback_cli_reads_the_profile_serial_and_prints_json(monkeypatch, capsys):
    """The CLI path builds its own BizReadback on the (possibly overridden) SERIAL."""
    from harness.verify import biz_readback

    serials = []

    class Reader(_FakeReader):
        def __init__(self, serial, settle_s=1.0):
            serials.append(serial)
            super().__init__()

    monkeypatch.setattr(business, "SERIAL", "emulator-5586")
    monkeypatch.setattr(biz_readback, "BizReadback", Reader)
    monkeypatch.setattr(biz_api, "from_env", lambda environ=None: None)
    business.main(["readback"])
    out = json.loads(capsys.readouterr().out)
    assert serials == ["emulator-5586"]
    assert out["hours"]["Northwind Traders"] == 4.25 and out["invoices_oracle"] == "invoice_ninja_screen_readback"
