"""One enforced run sequence: restore, seed, run, close, verify, store.

run_task is the only entry point and has no switches that skip a step:

    restore `baseline` -> seed plan + goal (task) -> apply seed -> pre-state
    -> RunRecord + trace -> run_agent (steps written as they happen)
    -> close the agent's device -> post-state -> verify (task) -> fill record
    -> finish trace -> render replay

The agent only ever holds a _GatedDevice; once closed every call on it
raises DeviceError, so nothing can act on the device after the agent's turn.
Any exception still finishes the trace with termination ERROR. A file lock
per serial in runs_dir makes runs queue one at a time.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from harness.agent.loop import AgentSettings, run_agent
from harness.contracts import (
    Config,
    ConfirmationDecision,
    ConfirmationHandler,
    ConfirmationRequest,
    Device,
    DeviceError,
    FlowType,
    RunRecord,
    Screenshot,
    StepRecord,
    TerminationReason,
)
from harness.seed.generator import apply, generate_plan, make_seed
from harness.tasks import registry
from harness.trace.replay import render_replay
from harness.trace.store import TraceStore, new_run_id
from harness.verify.runner import run_verifier

log = logging.getLogger(__name__)

BASELINE_SNAPSHOT = "baseline"
BASELINE_JSON = Path(__file__).resolve().parent / "emulator" / "baseline.json"
POLICIES = ("prompt", "approve", "reject")


class RunnerError(Exception):
    pass


class RunRefused(RunnerError):
    """The run must not start (e.g. freeform with host loopback open)."""


class RunnerBusy(RunnerError):
    """Another run holds this serial's queue lock."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- Device gate ----------------------------------------------------------------


class _GatedDevice:
    """Delegates to the real device until close(); afterwards every call raises."""

    def __init__(self, inner: Device) -> None:
        self._inner = inner
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()

    def _live(self) -> Device:
        if self._closed:
            raise DeviceError("device is closed: the agent's turn is over")
        return self._inner

    def screenshot(self) -> Screenshot:
        return self._live().screenshot()

    def ui_tree(self) -> str:
        return self._live().ui_tree()

    def tap(self, x: int, y: int) -> None:
        self._live().tap(x, y)

    def type_text(self, text: str) -> None:
        self._live().type_text(text)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._live().swipe(x1, y1, x2, y2, duration_ms)

    def back(self) -> None:
        self._live().back()

    def home(self) -> None:
        self._live().home()

    def open_app(self, package: str) -> None:
        self._live().open_app(package)

    def allowed_queries(self) -> list[str]:
        return self._live().allowed_queries()

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._live().query_structured(name, params)


# --- Confirmation policies --------------------------------------------------------


@dataclass(frozen=True)
class ConfirmPolicy:
    """`name` is what was asked for, `effective` what will actually answer."""

    name: str
    effective: str
    handler: ConfirmationHandler
    note: str = ""

    def meta(self) -> dict[str, Any]:
        out: dict[str, Any] = {"confirm_policy": self.name, "confirm_effective": self.effective}
        if self.note:
            out["confirm_note"] = self.note
        return out


def _fixed(decision: ConfirmationDecision) -> ConfirmationHandler:
    return lambda request: decision


def _prompt_handler(input_fn: Callable[[str], str], output: TextIO | None) -> ConfirmationHandler:
    def handler(request: ConfirmationRequest) -> ConfirmationDecision:
        out = output or sys.stdout
        print(f"\nThe agent asks to: {request.action}", file=out)
        print(json.dumps(request.summary, indent=2, ensure_ascii=False), file=out)
        out.flush()
        try:
            answer = input_fn("Approve? [y/N] ")
        except EOFError:
            answer = ""
        approved = answer.strip().lower() in ("y", "yes")
        return ConfirmationDecision.APPROVE if approved else ConfirmationDecision.REJECT

    return handler


def confirm_policy(
    name: str,
    *,
    input_fn: Callable[[str], str] = input,
    output: TextIO | None = None,
    is_tty: Callable[[], bool] | None = None,
) -> ConfirmPolicy:
    """Build a policy. `prompt` with no TTY on stdin becomes `reject`, so an
    unattended run can never approve silently."""
    if name == "approve":
        return ConfirmPolicy(name, name, _fixed(ConfirmationDecision.APPROVE))
    if name == "reject":
        return ConfirmPolicy(name, name, _fixed(ConfirmationDecision.REJECT))
    if name == "prompt":
        tty = is_tty() if is_tty is not None else sys.stdin.isatty()
        if not tty:
            return ConfirmPolicy(name, "reject", _fixed(ConfirmationDecision.REJECT), "stdin is not a tty")
        return ConfirmPolicy(name, name, _prompt_handler(input_fn, output))
    raise ValueError(f"unknown confirmation policy {name!r}; expected one of {POLICIES}")


def _as_policy(policy: str | ConfirmPolicy | ConfirmationHandler) -> ConfirmPolicy:
    if isinstance(policy, ConfirmPolicy):
        return policy
    if isinstance(policy, str):
        return confirm_policy(policy)
    if callable(policy):
        return ConfirmPolicy("custom", "custom", policy)
    raise TypeError(f"confirm_policy must be a policy name or a handler, not {type(policy).__name__}")


# --- Isolation checks ----------------------------------------------------------------


def host_loopback_blocked(path: str | Path = BASELINE_JSON) -> bool:
    """True only if baseline.json exists and says host_loopback_blocked: true."""
    try:
        return json.loads(Path(path).read_text()).get("host_loopback_blocked") is True
    except (OSError, ValueError, AttributeError):
        return False


@contextmanager
def serial_lock(runs_dir: str | Path, serial: str, timeout_s: float | None = None) -> Iterator[None]:
    """Exclusive lock for one serial. Blocks (the queue); with timeout_s,
    raises RunnerBusy once it passes."""
    path = Path(runs_dir) / f".queue-{serial}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        if timeout_s is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RunnerBusy(f"another run holds the queue for {serial}") from None
                    time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# --- The sequence ----------------------------------------------------------------------


def run_task(
    task_id: str | None,
    goal: str | None,
    config: Config,
    confirm_policy: str | ConfirmPolicy | ConfirmationHandler,
    *,
    device_factory: Callable[[], Device],
    inspector_factory: Callable[[], Any],
    emulator: Any,
    store: TraceStore,
    model_client: Any = None,
    seed: int | None = None,
    settings: AgentSettings | None = None,
    serial: str | None = None,
    lock_timeout_s: float | None = None,
    allow_unblocked: bool = False,
    baseline_json: str | Path | None = None,
    meta: dict[str, Any] | None = None,
) -> RunRecord:
    """Run one task (task_id) or one freeform goal (goal); exactly one is given.

    `emulator` needs restore_snapshot(name). Returns the finished RunRecord;
    failures inside the sequence are recorded as termination ERROR, not raised.
    """
    if (task_id is None) == (goal is None):
        raise ValueError("give exactly one of task_id or goal")
    task = registry.get(task_id) if task_id is not None else None
    policy = _as_policy(confirm_policy)
    baseline_json = BASELINE_JSON if baseline_json is None else baseline_json
    if task is None and not allow_unblocked and not host_loopback_blocked(baseline_json):
        raise RunRefused(
            f"freeform run refused: {baseline_json} does not say host_loopback_blocked is true "
            "(pass allow_unblocked to run anyway)"
        )
    serial = serial or getattr(emulator, "SERIAL", "default")
    run_meta = {**(meta or {}), **policy.meta(), "serial": serial}
    if task is None:
        run_meta["allow_unblocked"] = allow_unblocked
    with serial_lock(store.runs_dir, serial, lock_timeout_s):
        return _sequence(task, goal, config, policy, run_meta, device_factory, inspector_factory,
                         emulator, store, model_client, seed, settings)


def _sequence(task, goal, config, policy, run_meta, device_factory, inspector_factory,
              emulator, store, model_client, seed, settings) -> RunRecord:
    t0 = time.monotonic()
    started_at = _now()
    run_id = new_run_id()
    flow = task.flow_type if task is not None else FlowType.FREEFORM
    run_goal = goal if task is None else task.goal
    run: RunRecord | None = None
    writer = None
    gate: _GatedDevice | None = None
    steps: list[StepRecord] = []
    seed_used: int | None = None
    stage = "restore"
    error: BaseException | None = None
    try:
        emulator.restore_snapshot(BASELINE_SNAPSHOT)
        stage = "seed"
        inspector = inspector_factory()
        plan = None
        if task is not None:
            seed_used = make_seed() if seed is None else seed
            plan = generate_plan(seed_used, task)
            run_goal = registry.render_goal(task, plan)
            apply(plan, inspector)
        stage = "pre_state"
        pre = inspector.snapshot_state()
        stage = "trace"
        run = RunRecord(run_id=run_id, task_id=task.id if task else None, flow_type=flow, goal=run_goal,
                        model=config.model, started_at=started_at, meta=run_meta)
        writer = store.start_run(run)
        stage = "agent"
        gate = _GatedDevice(device_factory())

        def on_step(step: StepRecord, png: bytes | None, tree: str | None) -> None:
            steps.append(writer.write_step(step, png, tree))

        outcome = run_agent(run_goal, gate, config, policy.handler, on_step, model_client, settings=settings)
        gate.close()
        run.agent_verdict = outcome.verdict
        run.agent_summary = outcome.summary
        run.termination_reason = outcome.termination_reason
        run.usage = outcome.usage
        run.estimated_cost_usd = outcome.estimated_cost_usd
        stage = "post_state"
        post = inspector.snapshot_state()
        if task is not None:
            stage = "verify"
            run.verifier_result = run_verifier(task, plan, pre, post, run, steps)
    except BaseException as e:  # recorded below; non-Exceptions are re-raised after
        error = e
        log.exception("run %s failed during %s", run_id, stage)
    finally:
        if gate is not None:
            gate.close()

    if run is None:
        run = RunRecord(run_id=run_id, task_id=task.id if task else None, flow_type=flow, goal=run_goal,
                        model=config.model, started_at=started_at, meta=run_meta)
    if writer is None:
        writer = store.start_run(run)
    if error is not None:
        message = f"{stage}: {type(error).__name__}: {error}"
        run.termination_reason = TerminationReason.ERROR
        run.agent_summary = run.agent_summary or message
        run.meta["error"] = message
    run.ended_at = _now()
    run.seed = seed_used
    run.meta["wall_s"] = round(time.monotonic() - t0, 3)
    run.meta["steps"] = len(steps)
    writer.finish(run)
    try:
        render_replay(run_id, store.runs_dir)
    except Exception:
        log.exception("replay for run %s could not be rendered", run_id)
    if error is not None and not isinstance(error, Exception):
        raise error
    return run
