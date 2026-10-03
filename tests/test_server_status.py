"""GET /api/status: the single-device "in use" view for a second tester (fake mode, real HTTP)."""

import json
import threading
from datetime import datetime, timezone

import pytest

import harness.runner
from harness.contracts import Config, FlowType, RunRecord
from harness.fake_env import FakeEmulator
from harness.server.app import WAIT_HISTORY_RUNS, JobManager, StartJob
from harness.trace.store import TraceStore
from tests.test_server import TOKEN, Server, _start, _until, _wait_pending, srv  # noqa: F401
from tests.test_server_phase2 import _wait_status


def _history(srv, walls, *, fake=False, start=0):
    """Finished runs in the trace store with the given meta["wall_s"] values; ids sort by `start`."""
    store = TraceStore(srv.runs_dir)
    for i, wall in enumerate(walls, start):
        run = RunRecord(f"20260101-00{i:04d}-abcdef", "a_markor_note", FlowType.A, "g", "m",
                        "2026-01-01T00:00:00Z", meta={"wall_s": wall, "fake": fake})
        store.start_run(run)


def _status(srv):
    status, body = srv.json("/api/status")
    assert status == 200, body
    return body


def _wait_idle(srv):
    assert _until(lambda: _status(srv)["busy"] is False, 30), _status(srv)


def _answer(srv, job_id, decision):
    status, body = srv.json(f"/api/jobs/{job_id}/confirmation", method="POST", body={"decision": decision})
    assert status == 200, body


def _wait_pending_in_run(srv, job_id, n):
    """Wait until run n (1-based) of the job is paused on a confirmation."""
    assert _until(lambda: len(srv.json(f"/api/jobs/{job_id}")[1]["run_ids"]) == n
                  and srv.req(f"/api/jobs/{job_id}/confirmation")[0] == 200, 15)


def test_status_requires_the_token(srv):
    assert srv.req("/api/status", token=None)[0] == 401
    assert srv.req("/api/status", token="wrong")[0] == 401
    assert srv.req("/api/status", token=TOKEN)[0] == 200


def test_idle_status_has_no_current_job_and_no_estimate_without_history(srv):
    assert _status(srv) == {"busy": False, "current": None, "queued": 0, "queue": [], "restoring": False,
                            "median_run_s": None, "estimated_wait_s": None}


def test_busy_with_one_queued_shows_holder_name_steps_and_queue(srv):
    blocker = _start(srv, task_id="f_send_sms", policy="ui", name="  Alice   B ")
    _wait_pending(srv, blocker["job_id"])
    assert blocker["name"] == "Alice B"  # whitespace collapsed
    queued = _start(srv, task_id="a_markor_note", policy="approve", name="   ")
    assert queued["status"] == "queued" and queued["queue_position"] == 1 and queued["name"] is None

    s = _status(srv)
    job = srv.app.state.jobs.jobs[blocker["job_id"]]
    with job.cond:
        steps_emitted = sum(1 for e in job.events if e["event"] == "step")
    assert s["busy"] is True and s["restoring"] is False and s["queued"] == 1
    c = s["current"]
    assert c["job_id"] == blocker["job_id"] and c["task_id"] == "f_send_sms"
    assert c["requested_by_hint"] == "Alice B" and c["status"] == "waiting_confirmation"
    assert c["steps_so_far"] == steps_emitted > 0
    assert c["run_index"] == 1 and c["repeat"] == 1
    started = datetime.fromisoformat(c["started_at"])
    assert started.tzinfo is not None and abs((datetime.now(timezone.utc) - started).total_seconds()) < 60
    assert s["queue"] == [{"job_id": queued["job_id"], "task_id": "a_markor_note",
                           "requested_by_hint": "another tester"}]
    assert s["estimated_wait_s"] is None  # no non-fake history

    _answer(srv, blocker["job_id"], "approve")
    assert srv.events(queued["job_id"])[-1]["data"]["status"] == "done"
    _wait_idle(srv)
    assert _status(srv)["current"] is None

    store = TraceStore(srv.runs_dir)
    runs = {r.meta["job_id"]: r for r in (store.load_run(i)[0] for i in store.list_runs())}
    assert runs[blocker["job_id"]].meta["requested_by"] == "Alice B"
    assert "requested_by" not in runs[queued["job_id"]].meta
    # the fake runs recorded a wall time, but fake wall times never size the device wait
    assert all(r.meta["wall_s"] > 0 and r.meta["fake"] for r in runs.values())
    assert _status(srv)["median_run_s"] is None


def test_unnamed_freeform_job_shows_defaults(tmp_path):
    s = Server(tmp_path, allow_unblocked=True)
    try:
        job = _start(s, goal="Open Markor", policy="ui")  # fake freeform runs pause on a confirmation
        _wait_pending(s, job["job_id"])
        c = _status(s)["current"]
        assert c["task_id"] == "freeform" and c["requested_by_hint"] == "another tester"
        _answer(s, job["job_id"], "reject")
        _wait_idle(s)
    finally:
        s.stop()


def test_fake_jobs_add_nothing_to_the_wait_but_show_run_progress(srv):
    _history(srv, [100, 300, 200])
    _history(srv, [5, 5, 5, 5], fake=True, start=10)
    idle = _status(srv)
    assert idle["median_run_s"] == 200 and idle["estimated_wait_s"] == 0 and idle["busy"] is False

    blocker = _start(srv, task_id="f_send_sms", policy="ui", repeat=2)
    _wait_pending_in_run(srv, blocker["job_id"], 1)
    _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    s = _status(srv)
    assert s["busy"] is True and s["queued"] == 1
    assert s["current"]["run_index"] == 1 and s["current"]["repeat"] == 2
    assert s["estimated_wait_s"] == 0  # fake runs never hold the device for a real-run median

    _answer(srv, blocker["job_id"], "approve")
    _wait_pending_in_run(srv, blocker["job_id"], 2)
    assert _status(srv)["current"]["run_index"] == 2

    _history(srv, [1000, 1000], start=20)  # added runs change the cached median
    assert _status(srv)["median_run_s"] == 300
    _answer(srv, blocker["job_id"], "reject")
    _wait_idle(srv)


@pytest.fixture
def manager(tmp_path):
    """A JobManager whose worker is never started: submitted jobs stay queued and the
    test sets the active job's state, so real (non-fake) jobs are priced without a device."""
    jm = JobManager(Config(runs_dir=str(tmp_path / "runs"), api_key="unused"), allow_unblocked=False,
                    baseline_json=None, confirm_timeout_s=1.0)
    store = TraceStore(tmp_path / "runs")
    for i, wall in enumerate([100, 200, 300]):
        store.start_run(RunRecord(f"20260101-00{i:04d}-abcdef", "a_markor_note", FlowType.A, "g", "m",
                                  "2026-01-01T00:00:00Z", meta={"wall_s": wall, "fake": False}))
    return jm


def _activate(jm, job, run_ids, in_run):
    job.status = "running"
    job.run_ids = list(run_ids)
    job.run = RunRecord(run_ids[-1], job.req.task_id, FlowType.A, "g", "m", "t") if in_run else None


def test_wait_counts_remaining_real_repeats_and_queued_real_jobs(manager):
    active = manager.submit(StartJob(task_id="a_markor_note", repeat=3))
    manager.submit(StartJob(task_id="a_markor_note", repeat=2))
    manager.submit(StartJob(task_id="a_markor_note", repeat=5, fake=True))  # queued but free
    _activate(manager, active, ["r1"], in_run=True)
    s = manager.status()
    assert s["queued"] == 2 and s["median_run_s"] == 200
    assert s["estimated_wait_s"] == 200 * (3 + 2)  # runs 1-3 of the active job, then the real queued job
    _activate(manager, active, ["r1", "r2"], in_run=False)  # between runs 2 and 3
    assert manager.status()["estimated_wait_s"] == 200 * (1 + 2)


def test_worker_clears_the_finished_run_so_the_gap_between_repeats_is_not_double_counted(manager, monkeypatch):
    """Drives the real worker loop (not a hand-set state): at the start of each repeat the
    previous run is over, so only the repeats still to run are priced."""
    from harness import cli
    import harness.server.app as app_module

    seen = []
    real_run_task = app_module.run_task

    def spy(*args, **kwargs):
        seen.append(manager.status()["estimated_wait_s"])  # the gap: previous run done, next not started
        return real_run_task(*args, **kwargs)

    monkeypatch.setattr(app_module, "run_task", spy)
    # a real (priced) job whose runs use the fake device and scripted model
    monkeypatch.setattr(manager, "_env", lambda fake: cli.fake_env(manager.config))
    job = manager.submit(StartJob(task_id="a_markor_note", repeat=3, policy="approve"))
    manager.start()
    try:
        assert _until(lambda: job.status in ("done", "error", "cancelled"), 30), job.status
    finally:
        manager.stop()
    assert job.status == "done", job.error
    assert job.run is None and len(job.run_ids) == 3
    assert seen == [200 * 3, 200 * 2, 200 * 1]


def test_wait_after_cancel_counts_only_the_current_run(manager):
    active = manager.submit(StartJob(task_id="a_markor_note", repeat=5))
    manager.submit(StartJob(task_id="a_markor_note", repeat=1))
    _activate(manager, active, ["r1"], in_run=True)
    active.cancel = "fallback"  # unstarted repeats will never run
    assert manager.status()["estimated_wait_s"] == 200 * (1 + 1)
    _activate(manager, active, ["r1"], in_run=False)
    manager.restoring = True  # the server's restore after the cancel
    s = manager.status()
    assert s["restoring"] is True and s["busy"] is True and s["estimated_wait_s"] == 200 * (1 + 1)


def test_wait_for_an_active_fake_job_is_only_the_queued_real_work(manager):
    active = manager.submit(StartJob(task_id="a_markor_note", repeat=4, fake=True))
    manager.submit(StartJob(task_id="a_markor_note", repeat=1))
    _activate(manager, active, ["r1"], in_run=True)
    assert manager.status()["estimated_wait_s"] == 200


def test_median_skips_bad_wall_times_and_unreadable_runs(srv):
    _history(srv, [100, 300, -5, 0, True, "x", None, float("inf"), 1e308])
    bad = srv.runs_dir / "20260101-009998-abcdef"
    bad.mkdir()
    (bad / "run.json").write_text("{not json")
    null_meta = srv.runs_dir / "20260101-009999-abcdef"
    null_meta.mkdir()
    (null_meta / "run.json").write_text(json.dumps({"run_id": null_meta.name, "meta": None}))
    s = _status(srv)
    assert s["median_run_s"] == 200 and s["estimated_wait_s"] == 0


def test_median_uses_only_the_most_recent_runs(srv):
    _history(srv, [1000] * 25)
    _history(srv, [10] * WAIT_HISTORY_RUNS, start=100)  # newer ids
    assert _status(srv)["median_run_s"] == 10  # the 25 older runs would make it 1000


def test_cancelled_queued_job_leaves_the_queue(srv):
    blocker = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, blocker["job_id"])
    first = _start(srv, task_id="a_markor_note", policy="approve")
    second = _start(srv, task_id="a_markor_note", policy="approve")
    assert second["queue_position"] == 2
    srv.json(f"/api/jobs/{first['job_id']}/cancel", method="POST")
    s = _status(srv)
    assert s["queued"] == 1 and [q["job_id"] for q in s["queue"]] == [second["job_id"]]
    _answer(srv, blocker["job_id"], "reject")
    _wait_idle(srv)


@pytest.mark.parametrize("name, code", [("x" * 41, 422), (123, 422), (["a"], 422)])
def test_bad_names_are_refused(srv, name, code):
    status, _ = srv.json("/api/jobs", method="POST", body={"fake": True, "task_id": "a_markor_note", "name": name})
    assert status == code
    assert _status(srv)["busy"] is False and srv.app.state.jobs.jobs == {}


@pytest.mark.parametrize("name, stored", [
    ("y" * 40, "y" * 40),                                  # the maximum length is accepted
    ("<b>Al\x00ice</b>​\n\tB", "<b>Alice</b> B"),     # control/format chars dropped; HTML kept as text
])
def test_names_are_cleaned_and_reach_the_status(srv, name, stored):
    job = _start(srv, task_id="f_send_sms", policy="ui", name=name)
    _wait_pending(srv, job["job_id"])
    assert job["name"] == stored and _status(srv)["current"]["requested_by_hint"] == stored
    _answer(srv, job["job_id"], "reject")
    _wait_idle(srv)


def test_status_reports_restore_in_progress_after_a_cancel(srv, monkeypatch):
    # Without the runner's cancel hook the server's own fallback restores baseline,
    # which is the restore the server can observe (the runner's own restores are inside run_task).
    monkeypatch.delattr(harness.runner, "request_cancel")
    real, armed, gate = FakeEmulator.restore_snapshot, threading.Event(), threading.Event()

    def restore_snapshot(self, name):
        if armed.is_set() and not gate.wait(10):  # the restore after the cancel blocks
            raise RuntimeError("test never released the restore")
        return real(self, name)

    monkeypatch.setattr(FakeEmulator, "restore_snapshot", restore_snapshot)
    job = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, job["job_id"])
    armed.set()
    try:
        srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
        assert _until(lambda: _status(srv)["restoring"] is True, 15)
        s = _status(srv)
        assert s["busy"] is True and s["current"]["job_id"] == job["job_id"]
        assert srv.json(f"/api/jobs/{job['job_id']}")[1]["status"] == "running"  # not cancelled until restored
    finally:
        gate.set()
    _wait_status(srv, job["job_id"], "cancelled")
    s = _status(srv)
    assert s["restoring"] is False and s["busy"] is False
    assert srv.json(f"/api/jobs/{job['job_id']}")[1]["restore"] == "done"


def test_wall_time_written_when_a_run_ends_reaches_the_estimate(srv):
    """A run's id exists from its start but its wall_s only at its end; the cached median must see it."""
    _history(srv, [100])
    store = TraceStore(srv.runs_dir)
    run = RunRecord("20260101-000100-abcdef", "a_markor_note", FlowType.A, "g", "m",
                    "2026-01-01T00:01:00Z", meta={"fake": False})
    writer = store.start_run(run)  # in progress: no wall_s yet
    assert _status(srv)["median_run_s"] == 100
    run.meta["wall_s"] = 300
    run.ended_at = "2026-01-01T00:06:00Z"
    writer.finish(run)
    assert _status(srv)["median_run_s"] == 200
