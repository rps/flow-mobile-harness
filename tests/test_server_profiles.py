"""The web server runs business tasks on the business profile and everything
else on the harness emulator, keeping only one of the two emulators up.
Devices, inspectors and the model are fakes; the runner and the profile's
prepare_scored are real."""

import dataclasses

import pytest

from harness import cli
from harness.agent.loop import AgentSettings
from harness.contracts import Config, FlowType, RunRecord, VerifierResult, CheckResult, OracleTier, Verdict
from harness.emulator import manager as harness_manager
from harness.emulator.profiles import business
from harness.fake_env import FAKE_MODEL, ScriptedClient
from harness.server.app import JobManager, StartJob, _run_summary
from harness.tasks.sample_e_provider import STALE_ANSWER, TOTAL_CHECK
from harness.trace.store import TraceStore
from harness.verify import biz_api
from tests.agent_fakes import FakeDevice
from tests.biz_fakes import FakeBizInspector
from tests.test_server import _until


class Emulators:
    """Which emulator is up, and every start/stop the server asked for."""

    def __init__(self, harness_up=False, business_up=False):
        self.up = {"harness": harness_up, "business": business_up}
        self.calls: list[str] = []

    def stop(self, name):
        self.calls.append(f"stop {name}")
        self.up[name] = False

    def start(self, name):
        self.calls.append(f"start {name}")
        self.up[name] = True


@pytest.fixture
def emus(monkeypatch):
    e = Emulators()
    monkeypatch.setattr(harness_manager, "_online", lambda: e.up["harness"])
    monkeypatch.setattr(harness_manager, "stop", lambda: e.stop("harness"))
    monkeypatch.setattr(harness_manager, "start", lambda windowed=False: e.start("harness"))
    monkeypatch.setattr(business, "online", lambda: e.up["business"])
    monkeypatch.setattr(business, "stop", lambda: e.stop("business"))
    monkeypatch.setattr(business, "assert_scored", lambda task_id, goal: None)  # no business.json on this host
    monkeypatch.setattr(biz_api, "from_env", lambda environ=None: None)

    class BizEmu:
        SERIAL = "emulator-biz"

        @staticmethod
        def restore_snapshot(name):
            e.calls.append("restore business")

    def biz_env(config, windowed=True, invoice_api=None):
        if not e.up["business"]:
            e.start("business")
        return cli.Env(config=dataclasses.replace(config, model=FAKE_MODEL), emulator=BizEmu,
                       device_factory=FakeDevice, inspector_factory=FakeBizInspector,
                       model_client_factory=ScriptedClient,
                       settings=AgentSettings(allow_unpriced=True, run_reserve_usd=0.0))

    def harness_env(config, windowed):
        if not e.up["harness"]:
            e.start("harness")
        return cli.fake_env(config)

    monkeypatch.setattr(business, "env", biz_env)
    monkeypatch.setattr(cli, "real_env", harness_env)
    return e


@pytest.fixture
def jm(tmp_path):
    m = JobManager(Config(runs_dir=str(tmp_path / "runs"), api_key="unused"), allow_unblocked=False,
                   baseline_json=None, confirm_timeout_s=1.0)
    m.start()
    yield m
    m.stop()


def _finish(jm, **req):
    job = jm.submit(StartJob(policy="approve", **req))
    assert _until(lambda: job.status in ("done", "error", "cancelled"), 30), job.status
    assert job.status == "done", job.error
    store = TraceStore(jm.config.runs_dir)
    return [store.load_run(r)[0] for r in job.run_ids]


def test_real_business_task_runs_on_the_business_profile_after_stopping_the_harness_emulator(jm, emus):
    emus.up["harness"] = True
    (run,) = _finish(jm, task_id="biz_b2_rate")
    assert emus.calls == ["stop harness", "start business", "restore business"]
    assert emus.up == {"harness": False, "business": True}
    assert run.meta["profile"] == "business" and run.meta["serial"] == "emulator-biz"
    assert run.meta["source"] == "web_ui" and run.meta["fake"] is False
    assert "com.android.vending" in run.meta["blocked_packages"]  # the profile's Play Store block reached the runner
    assert run.meta["oracle"]["rate"] == "insightly_screen_readback"
    # verified against the business inspector's state: a real FAIL (no note written), not a check error
    failed = [c for c in run.verifier_result.end_state if not c.passed]
    assert failed and not any("check error" in c.detail for c in failed)


def test_harness_task_after_a_business_task_stops_the_business_emulator_and_restarts_its_own(jm, emus):
    _finish(jm, task_id="a_markor_note")  # first harness run: builds the harness env, which boots it
    _finish(jm, task_id="biz_h")
    emus.calls.clear()
    (run,) = _finish(jm, task_id="a_markor_note")
    assert emus.calls == ["stop business", "start harness"]
    assert emus.up == {"harness": True, "business": False}
    assert "profile" not in run.meta and "com.android.vending" not in run.meta["blocked_packages"]


def test_consecutive_runs_on_one_profile_do_not_restart_anything(jm, emus):
    _finish(jm, task_id="biz_h")
    emus.calls.clear()
    _finish(jm, task_id="biz_b1_hours", repeat=2)
    assert emus.calls == ["restore business", "restore business"]


def test_a_failed_stop_of_the_other_emulator_does_not_block_the_run(jm, emus, monkeypatch):
    emus.up["harness"] = True

    def boom():
        raise RuntimeError("adb gone")

    monkeypatch.setattr(harness_manager, "stop", boom)
    (run,) = _finish(jm, task_id="biz_h")
    assert run.meta["profile"] == "business"


def test_fake_business_run_touches_no_emulator(jm, emus):
    emus.up["harness"] = True
    (run,) = _finish(jm, task_id="biz_b2_rate", fake=True)
    assert emus.calls == [] and run.meta["fake"] is True and "profile" not in run.meta


def test_business_profile_refusal_surfaces_as_a_job_error(jm, emus, monkeypatch):
    def refuse(task_id, goal):
        raise business.ProfileError("cannot read profile baseline")

    monkeypatch.setattr(business, "assert_scored", refuse)
    job = jm.submit(StartJob(task_id="biz_h", policy="approve"))
    assert _until(lambda: job.status in ("done", "error"), 30)
    assert job.status == "error" and "cannot read profile baseline" in job.error and job.run_ids == []


# --- the Runs table's expected-FAIL flag ---------------------------------------


def _summary(tmp_path, task_id, checks, passed=False):
    run = RunRecord("20260101-000000-abcdef", task_id, FlowType.E, "g", "m", "t0", agent_verdict=Verdict.DONE)
    run.verifier_result = VerifierResult(passed=passed, oracle_tier=OracleTier.OWN_STORAGE, end_state=checks)
    return _run_summary(run, tmp_path, tags=[])


def test_run_summary_flags_only_the_designed_fallback_failure(tmp_path):
    stale = [CheckResult("note_created_with_title", True), CheckResult(TOTAL_CHECK, False, STALE_ANSWER)]
    assert _summary(tmp_path, "e_provider_fallback", stale)["expected_fail"] is True
    assert _summary(tmp_path, "e_provider_full", stale)["expected_fail"] is False
    missing = [CheckResult("note_created_with_title", False, "no new note"), CheckResult(TOTAL_CHECK, False, "no new note")]
    assert _summary(tmp_path, "e_provider_fallback", missing)["expected_fail"] is False
    ok = [CheckResult(TOTAL_CHECK, True, "total found")]
    assert _summary(tmp_path, "e_provider_fallback", ok, passed=True)["expected_fail"] is False
