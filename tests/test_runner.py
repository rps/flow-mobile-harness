import io
import json

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
