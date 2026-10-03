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
import threading
import time
from collections.abc import Callable, Iterable, Iterator
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
# Packages open_app refuses unless a freeform run allows them (scored tasks never do).
BLOCKED_PACKAGES = frozenset({"com.android.chrome"})
CHROME = "com.android.chrome"


class RunnerError(Exception):
    pass


class RunRefused(RunnerError):
    """The run must not start (e.g. freeform with host loopback open)."""


class RunnerBusy(RunnerError):
    """Another run holds this serial's queue lock."""


class RunCancelled(RunnerError):
    """request_cancel() was called for this run; raised at the next device call.

    Deliberately not a DeviceError: the agent loop would treat that as a
    retryable tool error and spend up to max_consecutive_device_errors more
    model calls before stopping. Unwinding past the loop loses the run's
    per-run usage figures (ledger.json still has every call), which the run
    meta notes.
    """


# --- Cancellation ---------------------------------------------------------------

class _Active:
    """One live run as request_cancel sees it."""

    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta
        self.gate: _GatedDevice | None = None  # set once the agent stage starts
        self.agent_done = False  # after this, a cancel can no longer take effect


_ACTIVE: dict[str, _Active] = {}  # cancel key -> run
_CANCELLED: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def request_cancel(key: str) -> bool:
    """Ask the run whose meta["job_id"] (or run_id) is `key` to stop.

    Takes effect at the run's next device call, at its next stage boundary
    (restore, seed, pre_state, agent) or while it waits for the serial lock:
    the trace finishes with termination ERROR and meta["cancelled"] = true,
    and the baseline snapshot is restored if the device was touched. A cancel
    during seeding still completes the seed and pre-state snapshot first.
    Returns True only if the cancel can still take effect; a request after the
    agent's turn (post-state, verify) returns False and is recorded as
    meta["cancel_requested_late"]. Unknown keys return False.
    """
    with _ACTIVE_LOCK:
        active = _ACTIVE.get(key)
        if active is None:
            return False
        if active.agent_done:
            active.meta["cancel_requested_late"] = True
            return False
        _CANCELLED.add(key)
        if active.gate is not None:
            active.gate.cancel()
    return True


def _is_cancelled(keys: Iterable[str]) -> bool:
    with _ACTIVE_LOCK:
        return any(k in _CANCELLED for k in keys)


def _register(keys: Iterable[str], active: _Active, gate: "_GatedDevice | None" = None) -> None:
    with _ACTIVE_LOCK:
        active.gate = gate
        for k in keys:
            _ACTIVE[k] = active
            if gate is not None and k in _CANCELLED:
                gate.cancel()


def _agent_done(keys: Iterable[str], gate: "_GatedDevice") -> bool:
    """Mark the agent stage over; returns True if a cancel was accepted before
    this moment (so the run must end as cancelled, not scored). Atomic with
    request_cancel: after this, requests are refused as late."""
    with _ACTIVE_LOCK:
        for k in keys:
            if k in _ACTIVE:
                _ACTIVE[k].agent_done = True
        return gate.cancelled


def _unregister(keys: Iterable[str]) -> None:
    with _ACTIVE_LOCK:
        for k in keys:
            _ACTIVE.pop(k, None)
            _CANCELLED.discard(k)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- Device gate ----------------------------------------------------------------


class _GatedDevice:
    """Delegates to the real device until close(); afterwards every call raises.
    cancel() makes every further call raise RunCancelled instead; open_app
    refuses packages in `blocked`."""

    def __init__(self, inner: Device, blocked: Iterable[str] = ()) -> None:
        self._inner = inner
        self._closed = False
        self._cancelled = False
        self._blocked = frozenset(blocked)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()

    def _live(self) -> Device:
        if self._cancelled:
            raise RunCancelled("run cancelled by request")
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
        dev = self._live()
        if package in self._blocked:
            raise DeviceError(f"{package} is not allowed in this run")
        dev.open_app(package)

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
def serial_lock(runs_dir: str | Path, serial: str, timeout_s: float | None = None,
                cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
    """Exclusive lock for one serial. Blocks (the queue); with timeout_s,
    raises RunnerBusy once it passes; with `cancelled`, the wait polls it and
    raises RunCancelled when it turns true."""
    path = Path(runs_dir) / f".queue-{serial}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        if timeout_s is None and cancelled is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = None if timeout_s is None else time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if cancelled is not None and cancelled():
                        raise RunCancelled("run cancelled by request while queued") from None
                    if deadline is not None and time.monotonic() >= deadline:
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
    allow_packages: Iterable[str] = (),
    windowed: bool = False,
) -> RunRecord:
    """Run one task (task_id) or one freeform goal (goal); exactly one is given.

    `emulator` needs restore_snapshot(name); if it also has stop() and
    start(windowed=) a failed restore is retried once after a cold boot
    (`windowed` is passed to that start; callers with a window pass True).
    `allow_packages` lifts BLOCKED_PACKAGES entries for a freeform run only.
    meta["job_id"], if given, is a key for request_cancel(). Returns the
    finished RunRecord; failures inside the sequence are recorded as
    termination ERROR, not raised.
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
    blocked = set(BLOCKED_PACKAGES)
    if task is None:
        blocked -= set(allow_packages)
    run_meta["blocked_packages"] = sorted(blocked)
    job_keys = [str(run_meta["job_id"])] if run_meta.get("job_id") is not None else []
    active = _Active(run_meta)
    _register(job_keys, active)  # cancellable while queued for the serial lock too
    args = (task, goal, config, policy, run_meta, device_factory, inspector_factory,
            emulator, store, model_client, seed, settings, blocked, windowed, job_keys, active, baseline_json)
    try:
        try:
            with serial_lock(store.runs_dir, serial, lock_timeout_s,
                             cancelled=(lambda: _is_cancelled(job_keys)) if job_keys else None):
                return _sequence(*args)
        except RunCancelled:
            # Cancelled while queued: _sequence sees the cancel before touching
            # the device and only writes the cancelled trace, so no lock is needed.
            return _sequence(*args)
    finally:
        _unregister(job_keys)


def _restore(emulator: Any, run_meta: dict[str, Any], windowed: bool) -> None:
    """restore_snapshot, retried once after stop + cold boot; the retry is
    recorded in meta["restore_retry"]."""
    try:
        emulator.restore_snapshot(BASELINE_SNAPSHOT)
        return
    except Exception as first:
        if not getattr(first, "retryable", True):
            raise  # e.g. the snapshot does not exist: a cold boot cannot help
        if not (callable(getattr(emulator, "stop", None)) and callable(getattr(emulator, "start", None))):
            raise
        log.warning("restore failed (%s); stopping and cold-booting the emulator once", first)
        retry: dict[str, Any] = {"error": f"{type(first).__name__}: {first}"}
        run_meta["restore_retry"] = retry
    emulator.stop()
    retry["boot_s"] = round(emulator.start(windowed=windowed), 1)
    emulator.restore_snapshot(BASELINE_SNAPSHOT)
    retry["ok"] = True


def _set_chrome(inspector: Any, blocked: set[str], run_meta: dict[str, Any]) -> None:
    """Second layer of the Chrome block: disable the package for this run (the
    next baseline restore brings it back), or re-enable it for a freeform run
    that allowed it. Records meta["chrome_disabled"] = True / False / reason.

    Fails closed: when Chrome must be blocked and the disable cannot be done,
    a DeviceError ends the run (termination ERROR) unless meta["fake"] says
    this is a fake-device run, which has no Chrome to block.
    """
    disable = CHROME in blocked
    shell = getattr(inspector, "shell", None)
    if not callable(shell):
        run_meta["chrome_disabled"] = "unsupported: inspector has no shell"
        if disable and not run_meta.get("fake"):
            raise DeviceError(f"cannot disable {CHROME}: inspector has no shell")
        return
    try:
        if disable:
            shell(["pm", "disable-user", "--user", "0", CHROME])
        else:
            shell(["pm", "enable", CHROME])
        run_meta["chrome_disabled"] = disable
    except Exception as e:
        run_meta["chrome_disabled"] = f"{type(e).__name__}: {e}"
        if disable:
            raise DeviceError(f"cannot disable {CHROME}, refusing to run scored: {e}") from e
        log.warning("could not enable %s: %s", CHROME, e)


def _check_baseline(emulator: Any, run_meta: dict[str, Any], baseline_json: str | Path) -> None:
    """Fail closed if the emulator can compare the run's baseline.json with the
    running device and finds drift (wrong AVD, image, missing snapshot, Markor
    sha). Emulators without the check (fakes) are recorded as unchecked."""
    check = getattr(emulator, "check_baseline_matches", None)
    if not callable(check):
        run_meta["baseline_check"] = "unavailable"
        return
    run_meta["baseline_check"] = check(Path(baseline_json))  # DeviceError on drift -> ERROR at stage restore


def _recheck_loopback(emulator: Any, run_meta: dict[str, Any]) -> None:
    """A freeform run trusts baseline.json's host_loopback_blocked. After a cold
    boot the emulator is a fresh process, so ask it again; fail closed if the
    probe is unavailable or the host is reachable."""
    probe = getattr(emulator, "loopback_reachable", None)
    if not callable(probe):
        run_meta["host_loopback_recheck"] = "unavailable"
        raise RunRefused("freeform run after a cold boot: emulator cannot re-check the host loopback block")
    reachable = probe()
    run_meta["host_loopback_recheck"] = "reachable" if reachable else "blocked"
    if reachable:
        raise RunRefused("freeform run refused: host loopback reachable after the cold boot")


def _sequence(task, goal, config, policy, run_meta, device_factory, inspector_factory,
              emulator, store, model_client, seed, settings, blocked, windowed, job_keys, active,
              baseline_json) -> RunRecord:
    t0 = time.monotonic()
    started_at = _now()
    run_id = new_run_id()
    cancel_keys = [run_id, *job_keys]
    _register(cancel_keys, active)

    def check_cancel() -> None:
        if _is_cancelled(cancel_keys):
            raise RunCancelled("run cancelled by request")
    flow = task.flow_type if task is not None else FlowType.FREEFORM
    run_goal = goal if task is None else task.goal
    run: RunRecord | None = None
    writer = None
    gate: _GatedDevice | None = None
    steps: list[StepRecord] = []
    seed_used: int | None = None
    stage = "restore"
    error: BaseException | None = None
    outcome = None
    try:
        check_cancel()
        _restore(emulator, run_meta, windowed)
        if task is None and "restore_retry" in run_meta and not run_meta.get("allow_unblocked"):
            _recheck_loopback(emulator, run_meta)
        _check_baseline(emulator, run_meta, baseline_json)
        stage = "seed"
        check_cancel()
        inspector = inspector_factory()
        _set_chrome(inspector, blocked, run_meta)
        plan = None
        if task is not None:
            seed_used = make_seed() if seed is None else seed
            plan = generate_plan(seed_used, task)
            run_goal = registry.render_goal(task, plan)
            apply(plan, inspector)
        stage = "pre_state"
        check_cancel()
        pre = inspector.snapshot_state()
        stage = "trace"
        run = RunRecord(run_id=run_id, task_id=task.id if task else None, flow_type=flow, goal=run_goal,
                        model=config.model, started_at=started_at, meta=run_meta)
        writer = store.start_run(run)
        stage = "agent"
        check_cancel()
        gate = _GatedDevice(device_factory(), blocked)
        _register(cancel_keys, active, gate)

        def on_step(step: StepRecord, png: bytes | None, tree: str | None) -> None:
            steps.append(writer.write_step(step, png, tree))

        outcome = run_agent(run_goal, gate, config, policy.handler, on_step, model_client, settings=settings)
        gate.close()
        run.usage = outcome.usage
        run.estimated_cost_usd = outcome.estimated_cost_usd
        if _agent_done(cancel_keys, gate):  # accepted cancel that the agent's last call did not hit
            raise RunCancelled("run cancelled by request")
        run.agent_verdict = outcome.verdict
        run.agent_summary = outcome.summary
        run.termination_reason = outcome.termination_reason
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
        _unregister(cancel_keys)

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
    if isinstance(error, RunCancelled):
        run.meta["cancelled"] = True
        if stage == "agent" and outcome is None:
            run.meta["usage_note"] = "cancelled mid-turn: per-run usage not captured; ledger.json has every call"
        if stage != "restore":  # the device was touched: put it back
            try:
                _restore(emulator, run.meta, windowed)
                run.meta["restored_after_cancel"] = True
            except Exception as e:
                log.exception("restore after cancelling run %s failed", run_id)
                run.meta["restored_after_cancel"] = f"{type(e).__name__}: {e}"
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
