import io
import json
import time

import pytest

import harness.runner as runner
from harness.agent.loop import AgentSettings
from harness.contracts import (
    Config,
    ConfirmationDecision,
    ConfirmationRequest,
    DeviceError,
    FlowType,
    RunRecord,
    TerminationReason,
)
from harness.fake_env import FAKE_MODEL, ScriptedClient
from harness.runner import RunnerBusy, RunRefused, confirm_policy, run_task, serial_lock
from harness.trace.store import TraceStore
from tests.agent_fakes import FakeDevice
from tests.fakes import FakeInspector

SETTINGS = AgentSettings(allow_unpriced=True)


class Recorder:
    """Shared call log, plus the emulator, factories and store that write to it."""

    SERIAL = "test-serial"

    def __init__(self, runs_dir):
        self.log: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self.inspector = FakeInspector()
        self.devices: list[RecordingDevice] = []
        self.agent_devices = []
        self.store = RecordingStore(runs_dir, self)
        self.restores: list[str] = []

    def mark(self, what):
        self.log.append(what)
        if what in self.fail:
            raise self.fail[what]

    def restore_snapshot(self, name):
        self.restores.append(name)
        self.mark("restore")
        return 0.0

    def inspector_factory(self):
        return RecordingInspector(self.inspector, self)

    def device_factory(self):
        d = RecordingDevice(self)
        self.devices.append(d)
        return d


class RecordingInspector:
    def __init__(self, inner, rec):
        self.inner, self.rec = inner, rec
        self.markor_dir = inner.markor_dir

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def insert_contact(self, *a, **kw):
        self.rec.mark("seed_write")
        return self.inner.insert_contact(*a, **kw)

    def snapshot_state(self):
        self.rec.mark("snapshot")
        return self.inner.snapshot_state()


class RecordingDevice(FakeDevice):
    def __init__(self, rec):
        super().__init__()
        self.rec = rec
        self.closed = False

    def close(self):
        self.closed = True
        self.rec.mark("device_close")


class RecordingStore(TraceStore):
    def __init__(self, runs_dir, rec):
        super().__init__(runs_dir)
        self.rec = rec
        self.initial_run_json = None

    def start_run(self, run):
        self.rec.mark("start_run")
        writer = super().start_run(run)
        self.initial_run_json = json.loads((self.run_dir(run.run_id) / "run.json").read_text())
        rec, finish = self.rec, writer.finish

        def recorded_finish(r):
            rec.mark("finish")
            finish(r)

        writer.finish = recorded_finish
        return writer


@pytest.fixture
def rec(tmp_path, monkeypatch):
    r = Recorder(tmp_path / "runs")
    real_agent, real_verifier, real_replay = runner.run_agent, runner.run_verifier, runner.render_replay

    def agent(goal, device, *a, **kw):
        r.agent_devices.append(device)
        r.mark("agent_start")
        out = real_agent(goal, device, *a, **kw)
        r.mark("agent_end")
        return out

    def verifier(*a, **kw):
        r.mark("verify")
        return real_verifier(*a, **kw)

    def replay(*a, **kw):
        r.mark("replay")
        return real_replay(*a, **kw)

    monkeypatch.setattr(runner, "run_agent", agent)
    monkeypatch.setattr(runner, "run_verifier", verifier)
    monkeypatch.setattr(runner, "render_replay", replay)
    return r


def _run(rec, task_id="a_markor_note", goal=None, policy="approve", client=None, **kw):
    config = Config(model=FAKE_MODEL, runs_dir=str(rec.store.runs_dir))
    return run_task(
        task_id, goal, config, policy,
        device_factory=rec.device_factory, inspector_factory=rec.inspector_factory,
        emulator=rec, store=rec.store, model_client=client or ScriptedClient(), settings=SETTINGS, **kw,
    )


def _on_disk(rec, run):
    return rec.store.load_run(run.run_id)


# --- sequence -----------------------------------------------------------------


def test_task_run_follows_the_enforced_order(rec):
    run = _run(rec, seed=7)
    assert [e for e in rec.log if e != "seed_write"] == [
        "restore", "snapshot", "start_run", "agent_start", "agent_end",
        "device_close", "snapshot", "verify", "finish", "replay",
    ]
    assert rec.log.index("restore") < rec.log.index("seed_write") < rec.log.index("snapshot")
    assert rec.restores == ["baseline"]
    assert run.flow_type is FlowType.A and run.seed == 7
    assert run.verifier_result is not None and run.verifier_result.passed is False
    assert run.termination_reason is TerminationReason.FINISHED


def test_agent_device_is_closed_before_verification_and_refuses_calls(rec):
    _run(rec)
    gate = rec.agent_devices[0]
    assert gate is not rec.devices[0]  # the agent never holds the raw device
    assert rec.devices[0].closed
    assert rec.log.index("device_close") < rec.log.index("verify")
    calls_before, frame_before = list(rec.devices[0].calls), rec.devices[0].frame
    with pytest.raises(DeviceError):
        gate.tap(1, 1)
    with pytest.raises(DeviceError):
        gate.screenshot()
    assert rec.devices[0].calls == calls_before and rec.devices[0].frame == frame_before


def test_seed_is_absent_from_trace_until_the_run_ends(rec):
    run = _run(rec, seed=99)
    assert rec.store.initial_run_json["seed"] is None
    stored, _ = _on_disk(rec, run)
    assert stored.seed == 99


def test_each_step_and_screenshot_is_written_and_replay_rendered(rec):
    run = _run(rec)
    stored, steps = _on_disk(rec, run)
    assert [s.tool_name for s in steps] == ["open_app", "tap", "type_text", "back", "finish"]
    run_dir = rec.store.run_dir(run.run_id)
    assert all((run_dir / s.screenshot_path).is_file() for s in steps)
    assert (run_dir / "replay.html").is_file()
    assert stored.meta["steps"] == 5 and stored.meta["wall_s"] >= 0
    assert stored.verifier_result == run.verifier_result


def test_goal_is_rendered_from_the_plan_and_sent_to_the_agent(rec):
    client = ScriptedClient()
    seen = []
    create = client.create

    def spy(**kw):
        seen.append(json.dumps(kw["messages"][0], default=str))
        return create(**kw)

    client.create = spy
    run = _run(rec, client=client, seed=3)
    assert "{" not in run.goal
    assert json.dumps(run.goal)[1:-1] in seen[0]


# --- errors -----------------------------------------------------------------------


@pytest.mark.parametrize("stage,label", [
    ("restore", "restore"),
    ("seed_write", "seed"),
    ("agent_start", "agent"),
    ("verify", "verify"),
])
def test_any_failure_still_finishes_the_trace_with_error(rec, stage, label):
    rec.fail = {stage: RuntimeError(f"boom at {stage}")}
    run = _run(rec)
    assert run.termination_reason is TerminationReason.ERROR
    assert run.meta["error"] == f"{label}: RuntimeError: boom at {stage}"
    stored, _ = _on_disk(rec, run)
    assert stored.termination_reason is TerminationReason.ERROR
    assert stored.ended_at is not None
    assert (rec.store.run_dir(run.run_id) / "replay.html").is_file()
    assert all(d.closed for d in rec.devices)
    if stage != "verify":
        assert "verify" not in rec.log


def test_model_client_crash_is_recorded_as_error(rec):
    class Broken:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ValueError("client exploded")

    run = _run(rec, client=Broken())
    assert run.termination_reason is TerminationReason.ERROR
    assert "client exploded" in run.agent_summary
    assert rec.devices[0].closed


def test_keyboard_interrupt_finishes_trace_then_propagates(rec):
    rec.fail = {"agent_start": KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        _run(rec)
    (run_id,) = rec.store.list_runs()
    stored, _ = rec.store.load_run(run_id)
    assert stored.termination_reason is TerminationReason.ERROR
    assert rec.devices[0].closed


def test_exactly_one_of_task_or_goal(rec):
    with pytest.raises(ValueError):
        _run(rec, task_id=None, goal=None)
    with pytest.raises(ValueError):
        _run(rec, task_id="a_markor_note", goal="x")
    assert rec.restores == []


# --- freeform ------------------------------------------------------------------------


def test_freeform_refused_without_blocked_loopback(rec, tmp_path):
    with pytest.raises(RunRefused):
        _run(rec, task_id=None, goal="open Markor", baseline_json=tmp_path / "missing.json")
    (tmp_path / "open.json").write_text(json.dumps({"host_loopback_blocked": False}))
    with pytest.raises(RunRefused):
        _run(rec, task_id=None, goal="open Markor", baseline_json=tmp_path / "open.json")
    assert rec.restores == [] and rec.store.list_runs() == []


def test_freeform_runs_without_seed_or_verifier(rec, tmp_path):
    (tmp_path / "b.json").write_text(json.dumps({"host_loopback_blocked": True}))
    run = _run(rec, task_id=None, goal="open Markor", baseline_json=tmp_path / "b.json")
    assert run.flow_type is FlowType.FREEFORM and run.task_id is None
    assert run.verifier_result is None and run.seed is None
    assert "seed_write" not in rec.log and "verify" not in rec.log
    assert rec.log[0] == "restore" and rec.devices[0].closed
    assert run.meta["allow_unblocked"] is False


def test_freeform_allow_unblocked_overrides_and_is_recorded(rec, tmp_path):
    run = _run(rec, task_id=None, goal="open Markor", baseline_json=tmp_path / "missing.json",
               allow_unblocked=True)
    assert run.meta["allow_unblocked"] is True
    assert run.termination_reason is TerminationReason.FINISHED


# --- serial queue ----------------------------------------------------------------------


def test_lock_held_by_another_run_blocks_start(rec):
    with serial_lock(rec.store.runs_dir, rec.SERIAL):
        with pytest.raises(RunnerBusy):
            _run(rec, lock_timeout_s=0.1)
    assert rec.restores == [] and rec.store.list_runs() == []
    _run(rec, lock_timeout_s=0)  # released afterwards
    assert rec.restores == ["baseline"]


def test_lock_is_per_serial(tmp_path):
    with serial_lock(tmp_path, "a"):
        with serial_lock(tmp_path, "b", timeout_s=0):
            pass
        with pytest.raises(RunnerBusy):
            with serial_lock(tmp_path, "a", timeout_s=0):
                pass


# --- confirmation policies -----------------------------------------------------------------

REQ = ConfirmationRequest(action="send sms", summary={"to": "Ana", "message": "hi"})


def test_fixed_policies():
    assert confirm_policy("approve").handler(REQ) is ConfirmationDecision.APPROVE
    assert confirm_policy("reject").handler(REQ) is ConfirmationDecision.REJECT
    with pytest.raises(ValueError):
        confirm_policy("maybe")


@pytest.mark.parametrize("answer,expected", [
    ("y", ConfirmationDecision.APPROVE),
    ("YES ", ConfirmationDecision.APPROVE),
    ("n", ConfirmationDecision.REJECT),
    ("", ConfirmationDecision.REJECT),
])
def test_prompt_policy_shows_summary_and_reads_answer(answer, expected):
    out = io.StringIO()
    p = confirm_policy("prompt", input_fn=lambda _: answer, output=out, is_tty=lambda: True)
    assert p.effective == "prompt"
    assert p.handler(REQ) is expected
    assert "send sms" in out.getvalue() and '"to": "Ana"' in out.getvalue()


def test_prompt_eof_rejects():
    def eof(_):
        raise EOFError

    p = confirm_policy("prompt", input_fn=eof, output=io.StringIO(), is_tty=lambda: True)
    assert p.handler(REQ) is ConfirmationDecision.REJECT


def test_prompt_without_tty_rejects_without_asking_and_says_so():
    asked = []
    p = confirm_policy("prompt", input_fn=lambda q: asked.append(q) or "y", is_tty=lambda: False)
    assert p.handler(REQ) is ConfirmationDecision.REJECT
    assert asked == []
    assert p.meta() == {"confirm_policy": "prompt", "confirm_effective": "reject",
                        "confirm_note": "stdin is not a tty"}


CONFIRM_SCRIPT = [
    ("request_confirmation", {"action": "send sms", "summary": {"to": "Ana"}}),
    ("finish", {"verdict": "done", "summary": "ok"}),
]


def test_policy_decision_reaches_agent_and_is_recorded(rec):
    run = _run(rec, policy=confirm_policy("prompt", is_tty=lambda: False), client=ScriptedClient(CONFIRM_SCRIPT))
    stored, steps = _on_disk(rec, run)
    assert steps[0].tool_result == {"decision": "reject"}
    assert stored.meta["confirm_policy"] == "prompt" and stored.meta["confirm_effective"] == "reject"


def test_custom_handler_is_used_and_recorded(rec):
    calls = []

    def handler(req):
        calls.append(req.action)
        return ConfirmationDecision.APPROVE

    run = _run(rec, policy=handler, client=ScriptedClient(CONFIRM_SCRIPT))
    _, steps = _on_disk(rec, run)
    assert calls == ["send sms"] and steps[0].tool_result == {"decision": "approve"}
    assert run.meta["confirm_policy"] == "custom"


# --- contracts: meta -------------------------------------------------------------------------


def test_run_record_meta_round_trips_and_defaults():
    r = RunRecord("r", None, FlowType.FREEFORM, "g", "m", "t", meta={"confirm_policy": "approve"})
    assert RunRecord.from_dict(json.loads(json.dumps(r.to_dict()))).meta == {"confirm_policy": "approve"}
    old = r.to_dict()
    del old["meta"]
    assert RunRecord.from_dict(old).meta == {}


# --- restore retry, blocked packages, cancel -----------------------------------


class ColdBootRecorder(Recorder):
    """Emulator that can stop() and start(); restore fails `restore_failures` times."""

    def __init__(self, runs_dir, restore_failures=1):
        super().__init__(runs_dir)
        self.restore_failures = restore_failures

    def restore_snapshot(self, name):
        self.restores.append(name)
        self.mark("restore")
        if self.restore_failures:
            self.restore_failures -= 1
            raise DeviceError("snapshot load: KO")
        return 0.0

    def stop(self):
        self.mark("stop")

    def start(self, windowed=False):
        self.mark(f"start windowed={windowed}")
        return 12.34


def test_failed_restore_is_retried_once_after_cold_boot(tmp_path, monkeypatch):
    rec = ColdBootRecorder(tmp_path / "runs")
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    run = _run(rec, windowed=True)
    assert rec.log[:4] == ["restore", "stop", "start windowed=True", "restore"]
    assert run.termination_reason is TerminationReason.FINISHED
    assert run.meta["restore_retry"] == {"error": "DeviceError: snapshot load: KO", "boot_s": 12.3, "ok": True}


def test_restore_retry_is_capped_at_one(tmp_path, monkeypatch):
    rec = ColdBootRecorder(tmp_path / "runs", restore_failures=2)
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    run = _run(rec)  # default: no window, like manager.start
    assert rec.log == ["restore", "stop", "start windowed=False", "restore", "start_run", "finish"]
    assert run.termination_reason is TerminationReason.ERROR
    assert run.meta["error"] == "restore: DeviceError: snapshot load: KO"
    assert run.meta["restore_retry"] == {"error": "DeviceError: snapshot load: KO", "boot_s": 12.3}


def test_emulator_without_cold_boot_fails_on_first_restore_error(rec):
    rec.fail = {"restore": DeviceError("KO")}
    run = _run(rec)
    assert run.meta["error"] == "restore: DeviceError: KO" and "restore_retry" not in run.meta


CHROME = [("open_app", {"package": "com.android.chrome"}), ("open_app", {"package": "net.gsantner.markor"}),
          ("finish", {"verdict": "done", "summary": "done"})]


def test_chrome_is_blocked_for_scored_tasks_even_if_allowed(rec):
    run = _run(rec, client=ScriptedClient(CHROME), allow_packages=["com.android.chrome"])
    assert run.meta["blocked_packages"] == ["com.android.chrome"]
    assert run.meta["chrome_disabled"] is True
    assert rec.inspector.shell_calls == [["pm", "disable-user", "--user", "0", "com.android.chrome"]]
    assert rec.log.index("restore") < rec.log.index("seed_write")  # disabled after restore, before seeding
    assert [c for c in rec.devices[0].calls if c[0] == "open_app"] == [("open_app", "net.gsantner.markor")]
    _, steps = _on_disk(rec, run)
    assert steps[0].tool_result == {"error": "open_app failed: com.android.chrome is not allowed in this run"}
    assert run.termination_reason is TerminationReason.FINISHED


def test_freeform_may_allow_chrome(rec, tmp_path):
    bj = tmp_path / "baseline.json"
    bj.write_text(json.dumps({"host_loopback_blocked": True}))
    run = _run(rec, task_id=None, goal="browse", client=ScriptedClient(CHROME), baseline_json=bj,
               allow_packages=["com.android.chrome"])
    assert run.meta["blocked_packages"] == [] and run.meta["chrome_disabled"] is False
    assert rec.inspector.shell_calls == [["pm", "enable", "com.android.chrome"]]
    assert rec.devices[0].calls[0] == ("open_app", "com.android.chrome")
    blocked = _run(rec, task_id=None, goal="browse", client=ScriptedClient(CHROME), baseline_json=bj)
    assert blocked.meta["blocked_packages"] == ["com.android.chrome"] and blocked.meta["chrome_disabled"] is True
    assert rec.devices[1].calls[0] == ("open_app", "net.gsantner.markor")


def test_chrome_disable_failure_fails_the_scored_run_closed(rec):
    def failing_shell(argv, timeout=30.0):
        raise DeviceError("pm: permission denied")

    rec.inspector.shell = failing_shell
    run = _run(rec)
    assert run.meta["chrome_disabled"] == "DeviceError: pm: permission denied"
    assert run.termination_reason is TerminationReason.ERROR
    assert run.meta["error"].startswith("seed: DeviceError: cannot disable com.android.chrome")
    assert "agent_start" not in rec.log and "verify" not in rec.log and run.verifier_result is None


def test_chrome_enable_failure_on_freeform_only_warns(rec, tmp_path):
    def failing_shell(argv, timeout=30.0):
        raise DeviceError("pm: permission denied")

    rec.inspector.shell = failing_shell
    bj = tmp_path / "baseline.json"
    bj.write_text(json.dumps({"host_loopback_blocked": True}))
    run = _run(rec, task_id=None, goal="browse", baseline_json=bj, allow_packages=["com.android.chrome"])
    assert run.meta["chrome_disabled"] == "DeviceError: pm: permission denied"
    assert run.termination_reason is TerminationReason.FINISHED


def test_inspector_without_shell_fails_scored_run_unless_fake(rec):
    rec.inspector.shell = None
    run = _run(rec)
    assert run.meta["chrome_disabled"] == "unsupported: inspector has no shell"
    assert run.termination_reason is TerminationReason.ERROR and "no shell" in run.meta["error"]
    fake_run = _run(rec, meta={"fake": True})
    assert fake_run.meta["chrome_disabled"] == "unsupported: inspector has no shell"
    assert fake_run.termination_reason is TerminationReason.FINISHED


class CancellingClient(ScriptedClient):
    """Cancels the run (as the web UI would, from another thread) on its 2nd call."""

    def __init__(self, key):
        super().__init__([("tap", {"x": 1, "y": 1})])
        self.key = key
        self.results = []

    def create(self, **kw):
        if self.calls == 1:
            self.results.append(runner.request_cancel(self.key))
        return super().create(**kw)


def test_cancel_during_agent_stops_at_next_device_call_and_restores(rec):
    client = CancellingClient("job-7")
    run = _run(rec, client=client, meta={"job_id": "job-7"})
    assert client.results == [True]
    assert client.calls == 2  # no third model call after the cancel
    assert run.termination_reason is TerminationReason.ERROR
    assert run.meta["error"] == "agent: RunCancelled: run cancelled by request"
    assert run.meta["cancelled"] is True and run.meta["restored_after_cancel"] is True
    assert rec.restores == ["baseline", "baseline"]
    assert "verify" not in rec.log and rec.devices[0].calls == [("tap", 1, 1)]
    stored, steps = _on_disk(rec, run)
    assert stored.termination_reason is TerminationReason.ERROR and len(steps) == 1
    assert run.meta["usage_note"].startswith("cancelled mid-turn")
    assert runner.request_cancel("job-7") is False  # nothing active afterwards


def test_cancel_before_agent_skips_restore_seed_and_agent(rec):
    class CancelOnLock:
        def __enter__(self):
            assert runner.request_cancel("job-8") is True

        def __exit__(self, *a):
            return False

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "serial_lock", lambda *a, **kw: CancelOnLock())
        run = _run(rec, meta={"job_id": "job-8"})
    assert run.meta["error"] == "restore: RunCancelled: run cancelled by request"
    assert run.meta["cancelled"] is True and "restored_after_cancel" not in run.meta
    assert "agent_start" not in rec.log and "seed_write" not in rec.log
    assert rec.restores == []  # device never touched, so nothing to put back
    assert rec.store.list_runs()  # but the cancelled run is on disk


def test_request_cancel_unknown_key_is_false():
    assert runner.request_cancel("no-such-job") is False


def test_cold_boot_failure_during_retry_is_recorded(tmp_path, monkeypatch):
    class BootFails(ColdBootRecorder):
        def start(self, windowed=False):
            raise DeviceError("emulator exited with 1")

    rec = BootFails(tmp_path / "runs")
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    run = _run(rec)
    assert run.meta["error"] == "restore: DeviceError: emulator exited with 1"
    assert run.meta["restore_retry"] == {"error": "DeviceError: snapshot load: KO"}
    assert "agent_start" not in rec.log


def test_restore_failure_after_cancel_is_recorded_as_string(rec):
    class CancelThenBreak(ScriptedClient):
        def create(self, **kw):
            runner.request_cancel("job-10")
            rec.fail = {"restore": DeviceError("KO after cancel")}
            return super().create(**kw)

    run = _run(rec, client=CancelThenBreak([("tap", {"x": 1, "y": 1})]), meta={"job_id": "job-10"})
    assert run.meta["cancelled"] is True
    assert run.meta["restored_after_cancel"] == "DeviceError: KO after cancel"  # no stop/start: no retry
    assert run.termination_reason is TerminationReason.ERROR


def test_cancel_during_seeding_ends_before_the_agent(rec):
    original = rec.inspector.insert_contact

    def cancelling_insert(*a, **kw):
        runner.request_cancel("job-9")
        return original(*a, **kw)

    rec.inspector.insert_contact = cancelling_insert
    run = _run(rec, meta={"job_id": "job-9"})
    assert run.meta["error"] == "pre_state: RunCancelled: run cancelled by request"
    assert run.meta["cancelled"] is True and run.meta["restored_after_cancel"] is True
    assert rec.devices == [] and "snapshot" not in rec.log and "verify" not in rec.log
    assert rec.restores == ["baseline", "baseline"]


def test_cancel_by_run_id(rec):
    class CancelByRunId(ScriptedClient):
        def create(self, **kw):
            (run_id,) = rec.store.list_runs()
            assert runner.request_cancel(run_id) is True
            return super().create(**kw)

    run = _run(rec, client=CancelByRunId([("tap", {"x": 1, "y": 1})]))
    assert run.meta["cancelled"] is True and rec.devices[0].calls == []


def test_cancel_while_queued_for_the_lock_returns_without_waiting(rec):
    import threading

    results = []
    with serial_lock(rec.store.runs_dir, rec.SERIAL):
        t = threading.Thread(target=lambda: results.append(_run(rec, meta={"job_id": "job-q"})))
        t.start()
        deadline = time.monotonic() + 5
        while not runner.request_cancel("job-q") and time.monotonic() < deadline:
            time.sleep(0.01)
        t.join(5)  # returns while the lock is still held
        assert not t.is_alive()
    (run,) = results
    assert run.meta["cancelled"] is True and run.meta["error"].startswith("restore: RunCancelled")
    assert rec.restores == [] and rec.devices == []


def test_serial_lock_cancel_callback(tmp_path):
    with serial_lock(tmp_path, "a"):
        with pytest.raises(runner.RunCancelled):
            with serial_lock(tmp_path, "a", cancelled=lambda: True):
                pass
        with pytest.raises(RunnerBusy):
            with serial_lock(tmp_path, "a", timeout_s=0, cancelled=lambda: False):
                pass


def test_cancel_after_the_agent_finished_has_no_effect(rec):
    class CancelInVerify:
        def __init__(self):
            self.result = None

    probe = CancelInVerify()
    real_verifier = runner.run_verifier

    def verifier(*a, **kw):
        probe.result = runner.request_cancel("job-v")
        return real_verifier(*a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "run_verifier", verifier)
        run = _run(rec, meta={"job_id": "job-v"})
    assert probe.result is False
    assert "cancelled" not in run.meta and run.termination_reason is TerminationReason.FINISHED
    assert run.meta["cancel_requested_late"] is True
    stored, _ = _on_disk(rec, run)
    assert stored.meta["cancel_requested_late"] is True


def test_restore_after_cancel_uses_the_cold_boot_retry(tmp_path, monkeypatch):
    rec = ColdBootRecorder(tmp_path / "runs", restore_failures=0)
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)

    class CancelThenBreak(ScriptedClient):
        def create(self, **kw):
            runner.request_cancel("job-11")
            rec.restore_failures = 1  # the post-cancel restore fails once
            return super().create(**kw)

    run = _run(rec, client=CancelThenBreak([("tap", {"x": 1, "y": 1})]), meta={"job_id": "job-11"})
    assert run.meta["cancelled"] is True and run.meta["restored_after_cancel"] is True
    assert run.meta["restore_retry"]["ok"] is True
    assert rec.log[-4:] == ["stop", "start windowed=False", "restore", "finish"]


class LoopbackRecorder(ColdBootRecorder):
    def __init__(self, runs_dir, reachable):
        super().__init__(runs_dir, restore_failures=1)
        self.reachable = reachable
        self.probes = 0

    def loopback_reachable(self):
        self.probes += 1
        return self.reachable


@pytest.mark.parametrize("reachable", [True, False])
def test_freeform_rechecks_loopback_after_a_cold_boot(tmp_path, monkeypatch, reachable):
    rec = LoopbackRecorder(tmp_path / "runs", reachable)
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    bj = tmp_path / "baseline.json"
    bj.write_text(json.dumps({"host_loopback_blocked": True}))
    run = _run(rec, task_id=None, goal="browse", baseline_json=bj)
    assert rec.probes == 1 and run.meta["restore_retry"]["ok"] is True
    if reachable:
        assert run.termination_reason is TerminationReason.ERROR
        assert run.meta["host_loopback_recheck"] == "reachable"
        assert "host loopback reachable" in run.meta["error"] and "agent_start" not in rec.log
    else:
        assert run.meta["host_loopback_recheck"] == "blocked"
        assert run.termination_reason is TerminationReason.FINISHED


def test_freeform_cold_boot_without_a_probe_is_refused(tmp_path, monkeypatch):
    rec = ColdBootRecorder(tmp_path / "runs")  # has stop/start but no loopback_reachable
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    bj = tmp_path / "baseline.json"
    bj.write_text(json.dumps({"host_loopback_blocked": True}))
    run = _run(rec, task_id=None, goal="browse", baseline_json=bj)
    assert run.meta["host_loopback_recheck"] == "unavailable"
    assert run.termination_reason is TerminationReason.ERROR and "agent_start" not in rec.log
    scored = _run(ColdBootRecorder(tmp_path / "runs2"))  # scored runs: snapshot state suffices
    assert "host_loopback_recheck" not in scored.meta and scored.termination_reason is TerminationReason.FINISHED


def test_cancel_accepted_during_the_agents_last_call_still_ends_cancelled(rec):
    class CancelThenFinish(ScriptedClient):
        def create(self, **kw):
            if self.calls == 1:
                assert runner.request_cancel("job-12") is True
            return super().create(**kw)

    client = CancelThenFinish([("tap", {"x": 1, "y": 1}),
                               ("finish", {"verdict": "done", "summary": "all good"})])
    run = _run(rec, client=client, meta={"job_id": "job-12"})
    assert run.meta["cancelled"] is True and run.termination_reason is TerminationReason.ERROR
    assert run.verifier_result is None and "verify" not in rec.log
    assert "usage_note" not in run.meta  # the completed turn's usage was captured


# --- baseline check at run start ----------------------------------------------------


def test_emulator_without_baseline_check_is_recorded_unchecked(rec):
    run = _run(rec)
    assert run.meta["baseline_check"] == "unavailable" and run.termination_reason is TerminationReason.FINISHED


def test_baseline_check_result_is_recorded_and_drift_fails_closed(rec):
    seen = []
    rec.check_baseline_matches = lambda path: seen.append(path) or {"avd": "p2", "snapshot": "baseline"}
    run = _run(rec)
    assert run.meta["baseline_check"] == {"avd": "p2", "snapshot": "baseline"}
    assert seen == [runner.BASELINE_JSON]  # the run's baseline_json, not a hard-coded path
    assert rec.log.index("restore") < rec.log.index("seed_write")

    def drifted(path):
        raise DeviceError("baseline does not match the device: running AVD 'old' != 'p2'")

    rec.check_baseline_matches = drifted
    bad = _run(rec)
    assert bad.termination_reason is TerminationReason.ERROR
    assert bad.meta["error"].startswith("restore: DeviceError: baseline does not match")
    assert bad.verifier_result is None and "agent_start" not in rec.log[rec.log.index("finish"):]


def test_non_retryable_restore_error_skips_the_cold_boot(tmp_path, monkeypatch):
    class NoSnapshot(ColdBootRecorder):
        def restore_snapshot(self, name):
            self.mark("restore")
            err = DeviceError("snapshot 'baseline' does not exist; provision first")
            err.retryable = False
            raise err

    rec = NoSnapshot(tmp_path / "runs")
    monkeypatch.setattr(runner, "render_replay", lambda *a, **kw: None)
    run = _run(rec)
    assert run.termination_reason is TerminationReason.ERROR
    assert "stop" not in rec.log and "restore_retry" not in run.meta
    assert run.meta["error"].startswith("restore: DeviceError: snapshot 'baseline' does not exist")
