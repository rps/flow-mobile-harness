"""Phase 2 web UI: job cancel, failure tags, run detail and step files (fake mode, real HTTP)."""

import json
import threading
import time
from contextlib import contextmanager

import pytest

import harness.runner
from harness.contracts import TerminationReason
from harness.fake_env import SCRIPT, FakeEmulator, ScriptedClient
from harness.scoreboard.aggregate import FAILURE_TAGS
from harness.server import app as app_module
from harness.trace.store import TraceStore
from tests.test_server import Server, _start, _until, _wait_pending, srv  # noqa: F401


def _wait_status(srv, job_id, status, timeout=15):
    assert _until(lambda: srv.json(f"/api/jobs/{job_id}")[1]["status"] == status, timeout), \
        srv.json(f"/api/jobs/{job_id}")[1]


def _wait_event(srv, job_id, kind, timeout=15):
    """Poll the job's event log until an event of `kind` exists; returns its index."""
    found = {}

    def check():
        job = srv.app.state.jobs.jobs[job_id]
        with job.cond:
            for i, ev in enumerate(job.events):
                if ev["event"] == kind:
                    found["i"] = i
                    return True
        return False

    assert _until(check, timeout), f"no {kind} event for {job_id}"
    return found["i"]


@pytest.fixture
def restores(monkeypatch):
    """Every restore_snapshot call across the fake emulators the worker creates."""
    real, calls = FakeEmulator.restore_snapshot, []

    def restore_snapshot(self, name):
        calls.append(name)
        return real(self, name)

    monkeypatch.setattr(FakeEmulator, "restore_snapshot", restore_snapshot)
    return calls


@pytest.fixture
def slow_model(monkeypatch):
    """Scripted model that takes 0.25 s per call, so a cancel can land between steps."""
    real = ScriptedClient.create

    def create(self, **kw):
        time.sleep(0.25)
        return real(self, **kw)

    monkeypatch.setattr(ScriptedClient, "create", create)


# --- cancel: server-side fallback (no runner hook, as on main) --------------------------


def test_cancel_at_confirmation_ends_run_with_error_trace_and_restores(srv, restores):
    job = _start(srv, task_id="f_send_sms", policy="ui", repeat=3)
    _wait_pending(srv, job["job_id"])  # blocked inside run 1 of 3

    status, body = srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    assert status == 200 and body["job_id"] == job["job_id"]
    evs = srv.events(job["job_id"])
    kinds = [e["event"] for e in evs]
    assert kinds.count("run_started") == 1 and kinds.count("run_finished") == 1, kinds  # runs 2 and 3 never start
    assert kinds.index("confirmation_resolved") < kinds.index("run_finished") < kinds.index("restore")
    resolved = next(e["data"] for e in evs if e["event"] == "confirmation_resolved")
    assert resolved["decision"] == "reject" and resolved["by"] == "cancel"
    restore = next(e["data"] for e in evs if e["event"] == "restore")
    assert restore == {"run_id": evs[-1]["data"]["run_ids"][0], "restore": "done", "by": "server"}
    done = evs[-1]["data"]
    assert done["status"] == "cancelled" and done["error"] is None and done["restore"] == "done"
    assert len(done["run_ids"]) == 1

    finished = next(e["data"] for e in evs if e["event"] == "run_finished")
    assert finished["termination_reason"] == "error" and "JobCancelled" in finished["error"]
    assert finished["cancelled"] is True and finished["restored_after_cancel"] is None
    run, steps = TraceStore(srv.runs_dir).load_run(done["run_ids"][0])
    assert run.termination_reason is TerminationReason.ERROR and run.ended_at is not None
    assert run.agent_verdict is None  # the harness ended the run, not the agent
    assert run.meta["job_id"] == job["job_id"] and run.meta["cancelled_by"] == app_module.CANCELLED_BY
    assert run.meta["ui_confirmations"] == [{"action": "send_sms", "decision": "reject", "by": "cancel"}]
    assert steps and steps[-1].tool_name == "request_confirmation"  # trace kept every step up to the cancel
    assert (srv.runs_dir / run.run_id / "replay.html").is_file()
    assert restores == ["baseline", "baseline"]  # run start, then after the cancel

    # a cancelled job is terminal: cancelling again or answering is refused
    assert srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")[0] == 409
    assert srv.json(f"/api/jobs/{job['job_id']}/confirmation", method="POST", body={"decision": "approve"})[0] == 409
    assert srv.req(f"/api/jobs/{job['job_id']}/confirmation")[0] == 204


def test_cancel_between_steps_raises_at_next_device_call(srv, slow_model):
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    _wait_event(srv, job["job_id"], "step")
    status, body = srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    assert status == 200 and body["status"] == "running" and body["cancel_requested"] is True  # stops at the next device call
    evs = srv.events(job["job_id"])
    kinds = [e["event"] for e in evs]
    assert "confirmation_resolved" not in kinds and kinds.count("run_started") == 1
    finished = next(e["data"] for e in evs if e["event"] == "run_finished")
    assert finished["termination_reason"] == "error" and "JobCancelled" in finished["error"]
    assert 0 < finished["steps"] < len(SCRIPT)
    assert evs[-1]["data"]["status"] == "cancelled" and evs[-1]["data"]["restore"] == "done"


def test_cancel_queued_job_ends_it_without_a_run(srv):
    blocker = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, blocker["job_id"])
    queued = _start(srv, task_id="a_markor_note", policy="approve")
    third = _start(srv, task_id="a_markor_note", policy="approve")
    assert srv.json(f"/api/jobs/{queued['job_id']}")[1]["status"] == "queued"

    status, body = srv.json(f"/api/jobs/{queued['job_id']}/cancel", method="POST")
    assert status == 200 and body["status"] == "cancelled" and body["run_ids"] == []
    assert srv.json(f"/api/jobs/{queued['job_id']}/cancel", method="POST")[0] == 409

    srv.json(f"/api/jobs/{blocker['job_id']}/confirmation", method="POST", body={"decision": "approve"})
    third_done = srv.events(third["job_id"])[-1]["data"]  # FIFO: the worker has passed the cancelled job
    assert third_done["status"] == "done" and len(third_done["run_ids"]) == 1
    assert srv.json(f"/api/jobs/{blocker['job_id']}")[1]["status"] == "done"
    st = srv.json(f"/api/jobs/{queued['job_id']}")[1]
    assert st["status"] == "cancelled" and st["run_ids"] == []
    assert [e["event"] for e in srv.events(queued["job_id"])] == ["job_done"]
    status, runs = srv.json("/api/runs")
    assert sorted(r["task_id"] for r in runs) == ["a_markor_note", "f_send_sms"]


def test_cancel_unknown_or_finished_job(srv):
    assert srv.req("/api/jobs/nope/cancel", method="POST")[0] == 404
    job = _start(srv, task_id="a_markor_note", policy="approve")
    srv.events(job["job_id"])
    status, body = srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    assert status == 409 and "done" in body["detail"]


def test_restore_after_cancel_reports_busy_lock(srv, monkeypatch, restores):
    @contextmanager
    def busy(*a, **kw):
        raise harness.runner.RunnerBusy("held by a test")
        yield  # noqa: unreachable, keeps it a generator

    monkeypatch.setattr(app_module, "serial_lock", busy)
    job = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, job["job_id"])
    srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    evs = srv.events(job["job_id"])
    restore = next(e["data"] for e in evs if e["event"] == "restore")
    assert restore["restore"] == "skipped_busy" and restore["by"] == "server"
    assert evs[-1]["data"]["status"] == "cancelled" and evs[-1]["data"]["restore"] == "skipped_busy"
    assert restores == ["baseline"]  # only the run's own restore


def test_restore_after_cancel_reports_failure(srv, monkeypatch):
    real = FakeEmulator.restore_snapshot
    fail = {"on": False}

    def restore_snapshot(self, name):
        if fail["on"]:
            raise RuntimeError("emulator died")
        return real(self, name)

    monkeypatch.setattr(FakeEmulator, "restore_snapshot", restore_snapshot)
    job = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, job["job_id"])
    fail["on"] = True
    srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    evs = srv.events(job["job_id"])
    restore = next(e["data"] for e in evs if e["event"] == "restore")
    assert restore["restore"] == "failed: RuntimeError: emulator died"
    assert evs[-1]["data"]["status"] == "cancelled" and evs[-1]["data"]["restore"].startswith("failed: ")


# --- cancel: the runner's own hook (area B) ------------------------------------------------


def test_runner_hook_true_owns_the_cancel(srv, monkeypatch, slow_model, restores):
    """request_cancel(job_id) -> True: the runner ends and restores the run, so the
    server neither raises in its wrappers nor restores. The fake hook here cannot
    end the run, so the run completes; the remaining repeats are still skipped."""
    calls = []
    monkeypatch.setattr(harness.runner, "request_cancel", lambda key: calls.append(key) or True, raising=False)
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    _wait_event(srv, job["job_id"], "step")
    assert srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")[0] == 200
    evs = srv.events(job["job_id"])
    kinds = [e["event"] for e in evs]
    assert calls == [job["job_id"]]
    assert kinds.count("run_started") == 1
    finished = next(e["data"] for e in evs if e["event"] == "run_finished")
    assert finished["termination_reason"] == "finished" and finished["steps"] == len(SCRIPT)  # wrappers stayed quiet
    restore = next(e["data"] for e in evs if e["event"] == "restore")  # belt and braces: the run was not stopped
    assert restore["by"] == "server" and restore["restore"] == "done" and "hook accepted" in restore["note"]
    assert evs[-1]["data"]["status"] == "cancelled" and evs[-1]["data"]["restore"] == "done"
    assert restores == ["baseline", "baseline"]


def test_runner_hook_false_falls_back_to_server_cancel(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(harness.runner, "request_cancel", lambda key: calls.append(key) or False, raising=False)
    job = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, job["job_id"])
    srv.json(f"/api/jobs/{job['job_id']}/cancel", method="POST")
    evs = srv.events(job["job_id"])
    assert calls == [job["job_id"]]
    finished = next(e["data"] for e in evs if e["event"] == "run_finished")
    assert finished["termination_reason"] == "error" and "JobCancelled" in finished["error"]
    restore = next(e["data"] for e in evs if e["event"] == "restore")
    assert restore["restore"] == "done" and restore["by"] == "server"


def test_runner_recorded_cancel_is_reported_without_server_restore(srv, monkeypatch, restores):
    """A run that comes back with meta cancelled/restored_after_cancel (the runner's
    bookkeeping) is shown as cancelled by the runner; the server does not restore."""
    real = app_module.run_task

    def run_task(*a, **kw):
        run = real(*a, **kw)
        run.meta["cancelled"] = True
        run.meta["restored_after_cancel"] = "restore_snapshot: emulator gone"
        return run

    monkeypatch.setattr(app_module, "run_task", run_task)
    monkeypatch.setattr(harness.runner, "request_cancel", lambda key: True, raising=False)
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=3)
    evs = srv.events(job["job_id"])
    restore = next(e["data"] for e in evs if e["event"] == "restore")
    assert restore["by"] == "runner" and restore["restore"] == "failed: restore_snapshot: emulator gone"
    finished = next(e["data"] for e in evs if e["event"] == "run_finished")
    assert finished["cancelled"] is True and finished["restored_after_cancel"] == "restore_snapshot: emulator gone"
    done = evs[-1]["data"]
    assert done["status"] == "cancelled" and len(done["run_ids"]) == 1  # repeats 2 and 3 skipped
    assert restores == ["baseline"]


@pytest.mark.parametrize("recorded, shown", [
    (True, "done"), (0.41, "done"), (3, "done"), ("restore_snapshot: emulator gone", "failed: restore_snapshot: emulator gone"),
    (None, "not_recorded"), (False, "not_recorded"), ("", "not_recorded"),
])
def test_runner_restore_state_mapping(recorded, shown):
    assert app_module._runner_restore_state(recorded) == shown


# --- failure tags ---------------------------------------------------------------------


def test_tags_roundtrip_shows_in_runs_and_scoreboard(srv):
    assert srv.json("/api/config")[1]["failure_tags"] == list(FAILURE_TAGS)
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    run_a, run_b = srv.events(job["job_id"])[-1]["data"]["run_ids"]

    status, body = srv.json(f"/api/runs/{run_a}/tags")
    assert status == 200 and body == {"run_id": run_a, "tags": [], "updated_at": None}

    status, body = srv.json(f"/api/runs/{run_a}/tags", method="PUT", body={"tags": ["login_wall", "wrong_element"]})
    assert status == 200 and body["tags"] == ["wrong_element", "login_wall"] and body["updated_at"]
    assert json.loads((srv.runs_dir / run_a / "tags.json").read_text())["tags"] == ["wrong_element", "login_wall"]

    by_id = {r["run_id"]: r for r in srv.json("/api/runs")[1]}
    assert by_id[run_a]["tags"] == ["wrong_element", "login_wall"] and by_id[run_b]["tags"] == []
    board = srv.json("/api/scoreboard")[1]["fake"]
    assert board["totals"]["failure_tags"]["login_wall"] == 1
    assert board["totals"]["failure_tags"]["lost_state"] == 0
    assert board["by_task"][0]["oracle_tier"] == 1  # smoke: a tier is shown although fake runs fail verification

    status, body = srv.json(f"/api/runs/{run_a}/tags", method="PUT", body={"tags": []})
    assert status == 200 and body["tags"] == []
    assert srv.json(f"/api/runs/{run_a}/tags")[1]["tags"] == []


def test_tags_reject_unknown_tag_and_unknown_run(srv):
    job = _start(srv, task_id="a_markor_note", policy="approve")
    run_id = srv.events(job["job_id"])[-1]["data"]["run_ids"][0]
    assert srv.req(f"/api/runs/{run_id}/tags", method="PUT", body={"tags": ["made_up"]})[0] == 422
    assert srv.req(f"/api/runs/{run_id}/tags", method="PUT", body={"tags": "login_wall"})[0] == 422
    assert not (srv.runs_dir / run_id / "tags.json").exists()
    assert srv.req("/api/runs/20260101-000000-abcdef/tags")[0] == 404
    assert srv.req("/api/runs/20260101-000000-abcdef/tags", method="PUT", body={"tags": []})[0] == 404


def test_folders_that_are_not_run_ids_are_never_served(srv):
    odd = srv.runs_dir / "not-a-run-id"
    odd.mkdir(parents=True)
    (odd / "run.json").write_text("{}")
    (odd / "step_000.txt").write_text("secret")
    assert srv.req("/api/runs/not-a-run-id/tags")[0] == 404
    assert srv.req("/api/runs/not-a-run-id/tags", method="PUT", body={"tags": []})[0] == 404
    assert srv.req("/api/runs/not-a-run-id")[0] == 404
    assert srv.req("/runs/not-a-run-id/step_000.txt")[0] == 404
    assert not (odd / "tags.json").exists()


# --- run detail and step files ----------------------------------------------------------


def test_run_detail_has_meta_trace_and_served_step_files(srv):
    job = _start(srv, task_id="a_markor_note", policy="approve")
    evs = srv.events(job["job_id"])
    run_id = evs[-1]["data"]["run_ids"][0]
    step0 = next(e["data"] for e in evs if e["event"] == "step")
    assert step0["ui_tree_path"] == "step_000.txt"  # live steps can link to their UI tree

    status, d = srv.json(f"/api/runs/{run_id}")
    assert status == 200 and d["run_id"] == run_id and d["steps"] == len(SCRIPT) and len(d["trace"]) == len(SCRIPT)
    assert d["run"]["meta"]["source"] == "web_ui" and d["run"]["meta"]["fake"] is True
    assert d["run"]["termination_reason"] == "finished" and d["tags"] == [] and d["cancelled"] is False
    assert d["trace"][0]["ui_tree_path"] == "step_000.txt" and d["trace"][0]["screenshot_path"] == "step_000.png"

    status, body, headers = srv.req(f"/runs/{run_id}/step_000.txt")
    assert status == 200 and headers["Content-Type"].startswith("text/plain")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert body == (srv.runs_dir / run_id / "step_000.txt").read_bytes()
    status, body, headers = srv.req(f"/runs/{run_id}/step_000.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body.startswith(b"\x89PNG")

    srv.json(f"/api/runs/{run_id}/tags", method="PUT", body={"tags": ["lost_state"]})
    assert (srv.runs_dir / run_id / "tags.json").is_file()
    assert srv.req(f"/runs/{run_id}/tags.json")[0] == 404  # only step files are served
    assert srv.req(f"/runs/{run_id}/run.json")[0] == 404
    assert srv.req(f"/runs/{run_id}/step_999.txt")[0] == 404
    (srv.runs_dir / run_id / "step_1000.txt").write_text("late step")
    assert srv.req(f"/runs/{run_id}/step_1000.txt")[0] == 200  # four-digit step files are served too
    assert srv.req("/runs/20260101-000000-abcdef/step_000.txt")[0] == 404
    assert srv.req("/api/runs/20260101-000000-abcdef")[0] == 404
    assert srv.req("/runs/x/step_000.txt", token=None)[0] == 401


def test_run_detail_with_unreadable_run_json_is_500_and_skipped_in_lists(srv):
    bad = srv.runs_dir / "20260101-000000-abcdef"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{broken")
    status, body = srv.json("/api/runs/20260101-000000-abcdef")
    assert status == 500 and "run.json unreadable" in body["detail"]
    status, runs = srv.json("/api/runs")
    assert status == 200 and runs == []
    assert srv.req("/api/runs/20260101-000000-abcdef/tags")[0] == 200  # tags do not need the record
