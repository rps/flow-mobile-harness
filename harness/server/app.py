"""Local web UI and API for the harness.

Every request needs the token: `?token=`, `Authorization: Bearer`, or the
HttpOnly cookie set when the page is loaded with `?token=`. Jobs (one task
or freeform goal, repeated N times) run one at a time in a single worker
thread, each run through runner.run_task, so the serial queue lock still
applies. The API key stays in Config inside this process; no response
carries it.

Policy `ui` blocks the run on a threading.Event until the page answers;
after `confirm_timeout_s` it resolves to reject and records that in the
run's meta["confirm_timeouts"].

Fake runs of flow-F tasks and freeform goals use a scripted model with one
request_confirmation step inserted by the server (not chosen by an agent),
so the approve/reject gate can be exercised without an emulator or API.

Cancel: every run is started with meta["job_id"]. A cancel first offers the
job id to `harness.runner.request_cancel` when that hook exists (outside the
job lock; no approval is accepted meanwhile), then in one locked step records
the answer, marks the job cancelled and resolves a waiting `ui` confirmation
to reject. If the hook took the cancel, the runner ends the run (termination
ERROR, meta "cancelled") and restores baseline itself (meta
"restored_after_cancel"). Otherwise the server's fallback is armed: the job's
wrapped device raises JobCancelled at its next call, the runner records
termination ERROR, finishes the trace and renders the replay, and the worker
restores `baseline` under the serial's queue lock (skipped if another process
holds it, because that run restores first anyway). A cancel that lands after
the agent's turn changes nothing about that run (it stands, scored as usual);
no further repeat starts and the job ends done with `cancel_requested` set so
the page can say so.
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import itertools
import json
import logging
import queue
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from harness.agent.pricing import estimate_cost, model_price
from harness.contracts import (
    Config,
    ConfirmationDecision,
    ConfirmationRequest,
    FlowType,
    RunRecord,
    StepRecord,
    TokenUsage,
)
from harness import runner as runner_module
from harness.runner import (
    BASELINE_SNAPSHOT,
    ConfirmPolicy,
    RunnerBusy,
    RunnerError,
    confirm_policy,
    host_loopback_blocked,
    run_task,
    serial_lock,
)
from harness.scoreboard.aggregate import FAILURE_TAGS, load_runs_with_tags, load_tags, save_tags, scoreboard
from harness.tasks import catalog, registry
from harness.trace.store import RunWriter, TraceStore

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "harness_ui_token"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
STEP_FILE_RE = re.compile(r"^step_\d{3,}\.(txt|png)$")
FAKE_CONFIRM_NOTE = "scripted fake step inserted by the server, not chosen by an agent"
TERMINAL = ("done", "error", "cancelled")
RESTORE_LOCK_TIMEOUT_S = 30.0
CANCELLED_BY = "web_ui"  # meta["cancelled_by"] when the server's own fallback stopped a run
CANCEL_DECIDED = ("runner", "fallback")


# --- Jobs ------------------------------------------------------------------------


class StartJob(BaseModel):
    task_id: str | None = None
    goal: str | None = None
    policy: Literal["ui", "approve", "reject"] = "ui"
    fake: bool = False
    repeat: int = Field(1, ge=1, le=20)

    @model_validator(mode="after")
    def _one_target(self) -> StartJob:
        if self.goal is not None:
            self.goal = self.goal.strip() or None
        if (self.task_id is None) == (self.goal is None):
            raise ValueError("give exactly one of task_id or goal")
        return self


class Answer(BaseModel):
    decision: ConfirmationDecision


class Tags(BaseModel):
    tags: list[Literal[FAILURE_TAGS]]  # type: ignore[valid-type]


class JobCancelled(Exception):
    """Raised inside a run by the server's wrappers once its job is cancelled."""


@dataclasses.dataclass
class Job:
    id: str
    req: StartJob
    status: str = "queued"
    run_ids: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    cond: threading.Condition = dataclasses.field(default_factory=threading.Condition)
    pending: ConfirmationRequest | None = None
    answer: ConfirmationDecision | None = None
    answer_by: str | None = None  # who set `answer`: ui, cancel or shutdown
    answered: threading.Event = dataclasses.field(default_factory=threading.Event)
    run: RunRecord | None = None
    # Cancel state, written under `cond`: None (no cancel), "asked" (hook being
    # asked), then one of "runner" (the runner's hook stops and restores the
    # run), "fallback" (the server's device wrapper stops it) or "queued" (the
    # job never ran).
    cancel: str | None = None
    restore: str | None = None

    def check_cancelled(self) -> None:
        """Fallback stop: raise once the server's fallback is armed (the hook
        is absent or declined). The run's meta records who cancelled, so
        nothing has to parse the error."""
        if self.cancel == "fallback":
            if self.run is not None:
                self.run.meta["cancelled_by"] = CANCELLED_BY
            raise JobCancelled("cancelled from the web UI")

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        with self.cond:
            self.events.append({"id": len(self.events), "event": kind, "data": data})
            self.cond.notify_all()

    def summary(self) -> dict[str, Any]:
        out = {
            "job_id": self.id, "status": self.status, "run_ids": list(self.run_ids), "error": self.error,
            "task_id": self.req.task_id, "goal": self.req.goal, "policy": self.req.policy,
            "fake": self.req.fake, "repeat": self.req.repeat, "restore": self.restore,
            "cancel_requested": self.cancel is not None,
        }
        if self.pending is not None:
            out["pending"] = self.pending.to_dict()
        return out


def _cost(model: str, usage: TokenUsage) -> float | None:
    return estimate_cost(model, usage) if model_price(model) is not None else None


def _request_runner_cancel(job: Job) -> bool:
    """`harness.runner.request_cancel(key: str) -> bool` (area B): every run
    is started with meta["job_id"], so the job id is the key. True means the
    runner owns this cancel: it ends the active run (termination ERROR,
    meta["cancelled"]) and restores baseline itself (meta["restored_after_cancel"]).
    The hook is present on main. False, an error, or a runner module without
    the hook (the no-hook case) leaves the server-side fallback in charge: the
    wrapped device raises at its next call and the worker restores baseline
    under the serial lock. Waived limitation of the
    fallback (not of the hook): the exception travels up through the agent
    loop, so the in-flight step's usage and cost are not accounted; the hook
    stops at the runner's gate and keeps them."""
    hook = getattr(runner_module, "request_cancel", None)
    if hook is None:
        return False
    try:
        return bool(hook(job.id))
    except Exception:
        log.exception("runner.request_cancel failed for job %s", job.id)
        return False


class _CancellableDevice:
    """Device wrapper: every call except close() and allowed_queries() checks
    for cancel first (the agent loop swallows an allowed_queries() error, so
    raising there would hide the cancel). The loop observes the screen after
    every step, so a cancel surfaces within one step."""

    def __init__(self, inner: Any, job: Job) -> None:
        self._inner, self._job = inner, job

    def close(self) -> None:
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "allowed_queries":
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self._job.check_cancelled()
            return attr(*args, **kwargs)

        return call


class _ObservedWriter(RunWriter):
    def __init__(self, run_dir: Path, job: Job, run: RunRecord) -> None:
        super().__init__(run_dir)
        self.job, self.run, self.usage = job, run, TokenUsage()

    def write_step(self, step: StepRecord, screenshot_png: bytes | None, ui_tree: str | None) -> StepRecord:
        stored = super().write_step(step, screenshot_png, ui_tree)
        self.usage = self.usage + step.usage
        self.job.emit("step", {
            "run_id": self.run.run_id,
            "index": stored.index,
            "tool_name": stored.tool_name,
            "tool_input": stored.tool_input,
            "tool_result": stored.tool_result,
            "reasoning": stored.reasoning,
            "ui_tree_path": stored.ui_tree_path,
            "screenshot_png_b64": base64.b64encode(screenshot_png).decode() if screenshot_png else None,
            "usage_so_far": self.usage.to_dict(),
            "cost_so_far_usd": _cost(self.run.model, self.usage),
        })
        return stored


class _ObservedStore(TraceStore):
    """A TraceStore that tells the job about the run and each step as written."""

    def __init__(self, runs_dir: str | Path, job: Job) -> None:
        super().__init__(runs_dir)
        self.job = job

    def start_run(self, run: RunRecord) -> RunWriter:
        writer = super().start_run(run)
        self.job.run = run
        self.job.run_ids.append(run.run_id)
        self.job.emit("run_started", {"run_id": run.run_id, "task_id": run.task_id,
                                      "flow_type": run.flow_type.value, "goal": run.goal})
        return _ObservedWriter(writer.run_dir, self.job, run)


def _fake_script(task_id: str | None, goal: str | None) -> list[tuple[str, dict[str, Any]]] | None:
    """The CLI's fake script, plus one request_confirmation before finish for
    flow-F tasks and freeform goals; None means the unchanged script."""
    from harness.fake_env import SCRIPT

    task = registry.get(task_id) if task_id else None
    if task is not None and task.flow_type is not FlowType.F:
        return None
    action = task.sensitive_actions[0] if task is not None and task.sensitive_actions else "fake_confirmation"
    step = ("request_confirmation", {"action": action, "summary": {
        "note": FAKE_CONFIRM_NOTE, "task_id": task_id, "goal": goal}})
    return [*SCRIPT[:-1], step, SCRIPT[-1]]


class JobManager:
    def __init__(self, config: Config, *, allow_unblocked: bool, baseline_json: str | Path | None,
                 confirm_timeout_s: float) -> None:
        self.config = config
        self.allow_unblocked = allow_unblocked
        self.baseline_json = baseline_json
        self.confirm_timeout_s = confirm_timeout_s
        self.jobs: dict[str, Job] = {}
        self.queue: queue.Queue[Job | None] = queue.Queue()
        self.closing = threading.Event()
        self._real_env = None
        self._counter = itertools.count(1)
        self.worker = threading.Thread(target=self._work, name="harness-ui-worker", daemon=True)

    # --- lifecycle

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.closing.set()
        for job in self.jobs.values():
            with job.cond:
                if job.pending is not None and not job.answered.is_set():
                    job.answer, job.answer_by = ConfirmationDecision.REJECT, "shutdown"
                    job.answered.set()
        self.queue.put(None)

    def freeform_enabled(self) -> bool:
        return self.allow_unblocked or host_loopback_blocked(*([self.baseline_json] if self.baseline_json else []))

    def submit(self, req: StartJob) -> Job:
        job = Job(id=f"job-{next(self._counter)}-{secrets.token_hex(3)}", req=req)
        self.jobs[job.id] = job
        self.queue.put(job)
        return job

    # --- confirmation

    def _ui_policy(self, job: Job) -> ConfirmPolicy:
        def handler(request: ConfirmationRequest) -> ConfirmationDecision:
            with job.cond:
                job.answered.clear()
                job.answer = job.answer_by = None
                job.pending = request
                job.status = "waiting_confirmation"
                if job.cancel in CANCEL_DECIDED:  # decided between the last check and now
                    job.answer, job.answer_by = ConfirmationDecision.REJECT, "cancel"
                    job.answered.set()
            job.emit("confirmation", {"run_id": job.run.run_id if job.run else None, **request.to_dict()})
            got = job.answered.wait(self.confirm_timeout_s)
            with job.cond:
                decision = job.answer if got and job.answer is not None else ConfirmationDecision.REJECT
                source = (job.answer_by or "ui") if got else "timeout"
                job.pending = None
                job.status = "running"
            if job.run is not None:
                job.run.meta.setdefault("ui_confirmations", []).append(
                    {"action": request.action, "decision": decision.value, "by": source})
                if not got:
                    job.run.meta.setdefault("confirm_timeouts", []).append(
                        {"action": request.action, "timeout_s": self.confirm_timeout_s})
            job.emit("confirmation_resolved", {"action": request.action, "decision": decision.value, "by": source})
            return decision

        return ConfirmPolicy("ui", "ui", handler)

    def cancel(self, job: Job) -> bool:
        """Cancel a queued or running job; False if it has already ended. A
        queued job ends at once; a running one at its next device call (or at
        the runner's own cancel gate when its hook takes the cancel)."""
        with job.cond:
            if job.status in TERMINAL:
                return False
            if job.cancel is not None:  # already stopping
                return True
            job.cancel = "asked"  # from here on answer() refuses approvals and no repeat starts
            running = job.status != "queued"
        # The runner's hook is asked before any other state changes and without
        # job.cond held (it may block; the worker needs the lock to emit). Then
        # one locked step records its answer, decides the cancel and rejects a
        # waiting confirmation, so the woken worker knows which path stops it.
        took_it = running and _request_runner_cancel(job)
        with job.cond:
            job.cancel = "runner" if took_it else "fallback" if running else "queued"
            if job.pending is not None:  # resolves to reject, even if already answered
                job.answer, job.answer_by = ConfirmationDecision.REJECT, "cancel"
                job.answered.set()
            if running:
                return True
            job.status = "cancelled"
        job.emit("job_done", job.summary())
        return True

    def answer(self, job: Job, decision: ConfirmationDecision) -> str | None:
        """Record the page's decision; returns the reason when it is refused."""
        with job.cond:
            if job.pending is None or job.answered.is_set():
                return "no confirmation is pending for this job"
            if job.cancel is not None:
                return "the job is being cancelled"
            job.answer, job.answer_by = decision, "ui"
            job.answered.set()
            return None

    # --- worker

    def _env(self, fake: bool):
        from harness import cli

        if fake:
            return cli.fake_env(self.config)
        if self._real_env is None:
            self._real_env = cli.real_env(self.config, windowed=False)
        return self._real_env

    def _work(self) -> None:
        while True:
            job = self.queue.get()
            if job is None or self.closing.is_set():
                return
            with job.cond:
                if job.cancel is not None:  # cancelled while queued; cancel() reports it
                    continue
                job.status = "running"
            try:
                interrupted = self._run_job(job)
            except Exception as e:  # a job error must not kill the worker
                log.exception("job %s failed", job.id)
                job.status, job.error = "error", f"{type(e).__name__}: {e}"
            else:
                job.status = "cancelled" if interrupted else "done"
            job.emit("job_done", job.summary())

    def _run_job(self, job: Job) -> bool:
        """Run the job's repeats; True if a cancel cut a run short or skipped
        one (a cancel that lands after the last run ended changes nothing)."""
        from harness.fake_env import ScriptedClient

        req = job.req
        env = self._env(req.fake)
        policy = self._ui_policy(job) if req.policy == "ui" else confirm_policy(req.policy)
        store = _ObservedStore(env.config.runs_dir, job)
        script = _fake_script(req.task_id, req.goal) if req.fake else None
        device_factory = lambda: _CancellableDevice(env.device_factory(), job)  # noqa: E731
        for _ in range(req.repeat):
            if self.closing.is_set():
                break
            if job.cancel is not None:
                return True
            client = ScriptedClient(script) if script is not None else env.model_client_factory()
            run = run_task(
                req.task_id, req.goal, env.config, policy,
                device_factory=device_factory, inspector_factory=env.inspector_factory,
                emulator=env.emulator, store=store, model_client=client, settings=env.settings,
                allow_unblocked=self.allow_unblocked, baseline_json=self.baseline_json,
                meta={"fake": env.fake, "source": "web_ui", "job_id": job.id,
                      **({"fake_confirm_step": FAKE_CONFIRM_NOTE} if script is not None else {})},
            )
            job.emit("run_finished", _run_summary(run, Path(env.config.runs_dir)))
            job.run = None
            if run.meta.get("cancelled"):  # the runner ended and restored it
                job.restore = _runner_restore_state(run.meta.get("restored_after_cancel"))
                job.emit("restore", {"run_id": run.run_id, "restore": job.restore, "by": "runner"})
                return True
            if run.meta.get("cancelled_by") == CANCELLED_BY:  # the server's fallback stopped it
                job.restore = self._restore(env)
                job.emit("restore", {"run_id": run.run_id, "restore": job.restore, "by": "server"})
                return True
            with job.cond:
                if job.cancel is not None:  # landed after the agent's turn: the run stands, no repeat starts
                    return False
        return False

    @staticmethod
    def _restore(env: Any) -> str:
        """Restore `baseline` after a cancelled run, under the serial's queue lock."""
        serial = getattr(env.emulator, "SERIAL", "default")
        try:
            with serial_lock(env.config.runs_dir, serial, RESTORE_LOCK_TIMEOUT_S):
                env.emulator.restore_snapshot(BASELINE_SNAPSHOT)
        except RunnerBusy:
            log.warning("restore after cancel skipped: another run holds the queue for %s", serial)
            return "skipped_busy"
        except Exception as e:
            log.exception("restore after cancel failed")
            return f"failed: {type(e).__name__}: {e}"
        return "done"


def _runner_restore_state(restored: Any) -> str:
    """meta["restored_after_cancel"] as the job's restore field: True (or a
    restore duration) is done, a string is the runner's error, nothing is
    not_recorded."""
    if restored is True or (isinstance(restored, (int, float)) and not isinstance(restored, bool)):
        return "done"
    if isinstance(restored, str) and restored:
        return f"failed: {restored}"
    return "not_recorded"


def _run_summary(run: RunRecord, runs_dir: Path, tags: list[str] | None = None) -> dict[str, Any]:
    vr = run.verifier_result
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "flow_type": run.flow_type.value,
        "goal": run.goal,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "agent_verdict": run.agent_verdict.value if run.agent_verdict else None,
        "agent_summary": run.agent_summary,
        "verifier_passed": None if vr is None else vr.passed,
        "oracle_tier": None if vr is None else int(vr.oracle_tier),
        "termination_reason": run.termination_reason.value if run.termination_reason else None,
        "steps": run.meta.get("steps"),
        "wall_s": run.meta.get("wall_s"),
        "cost_usd": run.estimated_cost_usd,
        "fake": bool(run.meta.get("fake")),
        "confirm_policy": run.meta.get("confirm_policy"),
        "confirm_timeouts": run.meta.get("confirm_timeouts", []),
        "error": run.meta.get("error"),
        "has_replay": (runs_dir / run.run_id / "replay.html").is_file(),
        "cancelled": bool(run.meta.get("cancelled")) or run.meta.get("cancelled_by") == CANCELLED_BY,
        "restored_after_cancel": run.meta.get("restored_after_cancel"),
        "tags": load_tags(runs_dir / run.run_id)["tags"] if tags is None else tags,
    }


# --- App ---------------------------------------------------------------------------


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token") or request.cookies.get(COOKIE)


def create_app(
    config: Config,
    token: str,
    *,
    allow_unblocked: bool = False,
    baseline_json: str | Path | None = None,
    confirm_timeout_s: float = 300.0,
    fake_default: bool = False,
) -> FastAPI:
    if not token:
        raise ValueError("a token is required")
    jobs = JobManager(config, allow_unblocked=allow_unblocked, baseline_json=baseline_json,
                      confirm_timeout_s=confirm_timeout_s)
    runs_dir = Path(config.runs_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title="harness", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        given = _token_from(request)
        if given is None or not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"detail": "token required"}, status_code=401)
        response = await call_next(request)
        if request.query_params.get("token") and request.url.path == "/":
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
        return response

    def _job(job_id: str) -> Job:
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/api/config")
    def ui_config() -> dict[str, Any]:
        enabled = jobs.freeform_enabled()
        note = "" if enabled else (
            "Freeform goals are disabled: the emulator baseline does not report host loopback as "
            "blocked (harness/emulator/baseline.json), so an agent could reach services on this Mac. "
            "Re-provision the baseline or start the server with --allow-unblocked.")
        return {"freeform_enabled": enabled, "freeform_note": note, "fake_default": fake_default,
                "confirm_timeout_s": confirm_timeout_s, "model": config.model,
                "fake_confirm_note": FAKE_CONFIRM_NOTE, "failure_tags": list(FAILURE_TAGS)}

    @app.get("/api/tasks")
    def tasks() -> list[dict[str, Any]]:
        return [{"id": t.id, "flow_type": t.flow_type.value, "goal_template": t.goal,
                 "oracle_tier": int(t.oracle_tier), "sensitive_actions": list(t.sensitive_actions),
                 **catalog.task_info(t.id)}
                for t in registry.all_tasks()]

    @app.get("/api/flows")
    def flows() -> list[dict[str, Any]]:
        return catalog.flows()

    @app.post("/api/jobs", status_code=201)
    def start_job(req: StartJob) -> dict[str, Any]:
        if req.task_id is not None and req.task_id not in {t.id for t in registry.all_tasks()}:
            raise HTTPException(422, f"unknown task {req.task_id!r}")
        if req.goal is not None and not jobs.freeform_enabled():
            raise HTTPException(409, "freeform run refused: host loopback is not blocked "
                                     "(start the server with --allow-unblocked to allow it)")
        return jobs.submit(req).summary()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [j.summary() for j in reversed(list(jobs.jobs.values()))]

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        return _job(job_id).summary()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, request: Request, after: int = -1) -> StreamingResponse:
        job = _job(job_id)
        last = request.headers.get("last-event-id")
        start = int(last) + 1 if last and last.isdigit() else after + 1

        def stream() -> Iterator[str]:
            i = max(0, start)
            while True:
                with job.cond:
                    if i >= len(job.events) and not jobs.closing.is_set():
                        job.cond.wait(timeout=15)
                    batch = job.events[i:]
                if not batch:
                    if jobs.closing.is_set():
                        return
                    yield ": keepalive\n\n"
                    continue
                for ev in batch:
                    yield f"id: {ev['id']}\nevent: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                    i = ev["id"] + 1
                    if ev["event"] == "job_done":
                        return

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        if not jobs.cancel(job):
            raise HTTPException(409, f"job already {job.status}")
        return job.summary()

    @app.get("/api/jobs/{job_id}/confirmation")
    def pending(job_id: str) -> Response:
        job = _job(job_id)
        req = job.pending
        if req is None:
            return Response(status_code=204)
        return JSONResponse({"job_id": job.id, "run_id": job.run.run_id if job.run else None, **req.to_dict()})

    @app.post("/api/jobs/{job_id}/confirmation")
    def answer(job_id: str, body: Answer) -> dict[str, Any]:
        job = _job(job_id)
        refused = jobs.answer(job, body.decision)
        if refused:
            raise HTTPException(409, refused)
        return {"job_id": job.id, "decision": body.decision.value}

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        out = [_run_summary(r, runs_dir, tags) for r, tags in load_runs_with_tags(runs_dir)]
        return sorted(out, key=lambda r: r["started_at"], reverse=True)

    @app.get("/runs/{run_id}/replay.html")
    def replay(run_id: str) -> FileResponse:
        path = runs_dir / run_id / "replay.html"
        if not RUN_ID_RE.match(run_id) or not path.is_file():
            raise HTTPException(404, "no replay for that run")
        return FileResponse(path, media_type="text/html", headers={"Referrer-Policy": "no-referrer"})

    @app.get("/runs/{run_id}/{name}")
    def step_file(run_id: str, name: str) -> FileResponse:
        """A step's UI-tree text (step_NNN.txt) or screenshot (step_NNN.png)."""
        path = runs_dir / run_id / name
        if not RUN_ID_RE.match(run_id) or not STEP_FILE_RE.match(name) or not path.is_file():
            raise HTTPException(404, "no such step file")
        media = "image/png" if name.endswith(".png") else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media,
                            headers={"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})

    def _run_dir(run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id) or not (runs_dir / run_id / "run.json").is_file():
            raise HTTPException(404, "unknown run")
        return runs_dir / run_id

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        """The full run record (including meta), its steps and tags; step
        paths are relative to the run folder and served under /runs/<id>/."""
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(404, "unknown run")
        try:
            run, steps = TraceStore(runs_dir).load_run(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "unknown run") from None
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise HTTPException(500, f"run.json unreadable: {type(e).__name__}") from e
        return {
            **_run_summary(run, runs_dir),
            "run": run.to_dict(),
            "trace": [s.to_dict() for s in steps],  # "steps" above stays the count
        }

    @app.get("/api/runs/{run_id}/tags")
    def get_tags(run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, **load_tags(_run_dir(run_id))}

    @app.put("/api/runs/{run_id}/tags")
    def put_tags(run_id: str, body: Tags) -> dict[str, Any]:
        return {"run_id": run_id, **save_tags(_run_dir(run_id), list(body.tags))}

    @app.get("/api/scoreboard")
    def board() -> dict[str, Any]:
        return scoreboard(runs_dir)

    return app

