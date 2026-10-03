"""Web UI server, driven over real HTTP with the standard library (fake mode only)."""

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from harness.contracts import Config
from harness.server.app import FAKE_CONFIRM_NOTE, create_app
from harness.tasks import catalog
from harness.trace.store import TraceStore

TOKEN = "test-token-123"


class Server:
    def __init__(self, tmp_path: Path, **kw):
        self.runs_dir = tmp_path / "runs"
        kw.setdefault("baseline_json", tmp_path / "missing-baseline.json")
        self.app = create_app(Config(runs_dir=str(self.runs_dir), api_key="sk-secret-never-sent"), TOKEN, **kw)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        assert _until(lambda: self.server.started, 10)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)

    def req(self, path, *, method="GET", body=None, token=TOKEN, headers=None, timeout=10):
        url = f"http://127.0.0.1:{self.port}{path}"
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, raw, resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, path, **kw):
        status, raw, _ = self.req(path, **kw)
        return status, (json.loads(raw) if raw else None)

    def events(self, job_id, until="job_done", timeout=30):
        """Read the SSE stream line by line until the `until` event."""
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/jobs/{job_id}/events",
                                   headers={"Authorization": f"Bearer {TOKEN}"})
        out, ev = [], {}
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            for raw in resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    ev["event"] = line[7:]
                elif line.startswith("data: "):
                    ev["data"] = json.loads(line[6:])
                elif line == "" and ev:
                    out.append(ev)
                    if ev["event"] == until:
                        return out
                    ev = {}
        return out


def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    yield s
    s.stop()


def _start(srv, **body):
    status, job = srv.json("/api/jobs", method="POST", body={"fake": True, **body})
    assert status == 201, job
    return job


# --- auth -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/tasks", "/api/flows", "/api/scoreboard", "/api/jobs/x/events",
                                  "/runs/20260101-000000-abcdef/replay.html"])
def test_every_route_requires_the_token(srv, path):
    assert srv.req(path, token=None)[0] == 401
    assert srv.req(path, token="wrong")[0] == 401


def test_token_by_query_sets_cookie_and_cookie_then_works(srv):
    status, body, headers = srv.req(f"/?token={TOKEN}", token=None)
    assert status == 200 and b"Harness Console" in body
    cookie = headers["set-cookie"]
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    pair = cookie.split(";")[0]
    assert srv.req("/api/tasks", token=None, headers={"Cookie": pair})[0] == 200
    assert srv.req("/api/tasks", token=None, headers={"Cookie": "harness_ui_token=nope"})[0] == 401


def test_api_key_never_appears_in_responses(srv):
    job = _start(srv, task_id="a_markor_note", policy="approve")
    srv.events(job["job_id"])
    for path in ["/api/config", "/api/tasks", "/api/runs", "/api/scoreboard", f"/api/jobs/{job['job_id']}"]:
        assert b"sk-secret-never-sent" not in srv.req(path)[1]


# --- tasks and validation ------------------------------------------------------------


def test_tasks_list_ids_flows_and_goal_templates(srv):
    status, tasks = srv.json("/api/tasks")
    assert status == 200
    by_id = {t["id"]: t for t in tasks}
    assert by_id["f_send_sms"]["flow_type"] == "f"
    assert "{message}" in by_id["f_send_sms"]["goal_template"]
    assert by_id["c_variant_b"]["label"] == "[Flow C] Checkout on an Alternate UI"
    assert by_id["c_variant_b"]["flow_name"] == "UI Variation Robustness"
    assert {t["id"] for t in tasks} == set(catalog.TASKS)


def test_flows_list_every_flow_with_its_tasks(srv):
    status, flows = srv.json("/api/flows")
    assert status == 200
    by_flow = {f["flow_type"]: f for f in flows}
    assert set(by_flow) == {f.value for f in catalog.FLOWS}
    assert by_flow["b"]["label"] == "[Flow B] Multi-App Information Transfer"
    status, tasks = srv.json("/api/tasks")
    assert status == 200
    nested = {t["id"]: (f["flow_type"], t) for f in flows for t in f["tasks"]}
    assert {k: v[0] for k, v in nested.items()} == {t["id"]: t["flow_type"] for t in tasks}
    for t in tasks:
        n = nested[t["id"]][1]
        assert (n["label"], n["description"], n["oracle_tier"]) == (t["label"], t["description"], t["oracle_tier"])


@pytest.mark.parametrize("body", [{}, {"task_id": "a_markor_note", "goal": "x"}, {"task_id": "nope"},
                                  {"task_id": "a_markor_note", "policy": "prompt"},
                                  {"task_id": "a_markor_note", "repeat": 0}])
def test_bad_start_requests_are_422(srv, body):
    assert srv.json("/api/jobs", method="POST", body=body)[0] == 422


def test_freeform_refused_and_disabled_when_loopback_not_blocked(srv):
    status, cfg = srv.json("/api/config")
    assert cfg["freeform_enabled"] is False and "--allow-unblocked" in cfg["freeform_note"]
    status, body = srv.json("/api/jobs", method="POST", body={"goal": "do a thing", "fake": True})
    assert status == 409 and "loopback" in body["detail"]


def test_freeform_enabled_when_baseline_blocks_loopback(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"host_loopback_blocked": True}))
    s = Server(tmp_path, baseline_json=baseline)
    try:
        assert s.json("/api/config")[1]["freeform_enabled"] is True
        job = _start(s, goal="Open Markor", policy="approve")
        evs = s.events(job["job_id"])
        finished = [e for e in evs if e["event"] == "run_finished"]
        assert finished[0]["data"]["task_id"] is None and finished[0]["data"]["verifier_passed"] is None
    finally:
        s.stop()


def test_allow_unblocked_flag_enables_freeform(tmp_path):
    s = Server(tmp_path, allow_unblocked=True)
    try:
        assert s.json("/api/config")[1]["freeform_enabled"] is True
    finally:
        s.stop()


# --- runs and streaming --------------------------------------------------------------


def test_fake_run_streams_every_step_and_finishes(srv):
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    evs = srv.events(job["job_id"])
    steps = [e["data"] for e in evs if e["event"] == "step"]
    assert [s["index"] for s in steps] == [0, 1, 2, 3, 4] * 2
    assert len(steps) == 10  # 5 scripted steps x 2 runs
    assert steps[0]["tool_name"] == "open_app" and steps[4]["tool_name"] == "finish"
    assert base64.b64decode(steps[0]["screenshot_png_b64"]).startswith(b"\x89PNG")
    assert set(steps[0]["usage_so_far"]) >= {"input_tokens", "output_tokens"}
    assert "cost_so_far_usd" in steps[0] and steps[0]["cost_so_far_usd"] is None  # fake model is unpriced
    assert [e["event"] for e in evs].count("run_finished") == 2

    status, st = srv.json(f"/api/jobs/{job['job_id']}")
    assert st["status"] == "done" and len(st["run_ids"]) == 2
    store = TraceStore(srv.runs_dir)
    run, stored = store.load_run(st["run_ids"][0])
    assert run.meta["fake"] is True and run.meta["source"] == "web_ui" and len(stored) == 5

    status, runs = srv.json("/api/runs")
    assert {r["run_id"] for r in runs} == set(st["run_ids"]) and all(r["has_replay"] for r in runs)
    status, body, headers = srv.req(f"/runs/{st['run_ids'][0]}/replay.html")
    assert status == 200 and headers["Content-Type"].startswith("text/html") and st["run_ids"][0].encode() in body


def test_replay_rejects_bad_or_unknown_run_ids(srv):
    assert srv.req("/runs/..%2F..%2Fetc/replay.html")[0] == 404
    assert srv.req("/runs/20260101-000000-abcdef/replay.html")[0] == 404


def test_unknown_job_is_404(srv):
    assert srv.req("/api/jobs/nope")[0] == 404
    assert srv.req("/api/jobs/nope/confirmation", method="POST", body={"decision": "approve"})[0] == 404


# --- confirmation gate -------------------------------------------------------------------


def _wait_pending(srv, job_id):
    got = {}

    def check():
        status, body = srv.json(f"/api/jobs/{job_id}/confirmation")
        if status == 200:
            got.update(body)
        return status == 200

    assert _until(check, 15), "run never asked for confirmation"
    return got


def test_ui_policy_pauses_until_approved(srv):
    job = _start(srv, task_id="f_send_sms", policy="ui")
    pending = _wait_pending(srv, job["job_id"])
    assert pending["action"] == "send_sms" and pending["summary"]["note"] == FAKE_CONFIRM_NOTE
    status, st = srv.json(f"/api/jobs/{job['job_id']}")
    assert st["status"] == "waiting_confirmation" and st["pending"]["action"] == "send_sms"
    time.sleep(0.3)
    assert srv.json(f"/api/jobs/{job['job_id']}")[1]["status"] == "waiting_confirmation"  # still blocked

    status, body = srv.json(f"/api/jobs/{job['job_id']}/confirmation", method="POST", body={"decision": "approve"})
    assert status == 200 and body["decision"] == "approve"
    evs = srv.events(job["job_id"])
    kinds = [e["event"] for e in evs]
    assert kinds.index("confirmation") < kinds.index("confirmation_resolved") < kinds.index("job_done")
    confirm_step = next(e["data"] for e in evs if e["event"] == "step" and e["data"]["tool_name"] == "request_confirmation")
    assert confirm_step["tool_result"]["decision"] == "approve"
    run, _ = TraceStore(srv.runs_dir).load_run(evs[-1]["data"]["run_ids"][0])
    assert run.meta["ui_confirmations"] == [{"action": "send_sms", "decision": "approve", "by": "ui"}]
    assert "confirm_timeouts" not in run.meta and run.meta["confirm_policy"] == "ui"
    assert srv.req(f"/api/jobs/{job['job_id']}/confirmation")[0] == 204


def test_ui_policy_reject_and_double_answer(srv):
    job = _start(srv, task_id="f_send_sms", policy="ui")
    _wait_pending(srv, job["job_id"])
    assert srv.json(f"/api/jobs/{job['job_id']}/confirmation", method="POST", body={"decision": "reject"})[0] == 200
    evs = srv.events(job["job_id"])
    step = next(e["data"] for e in evs if e["event"] == "step" and e["data"]["tool_name"] == "request_confirmation")
    assert step["tool_result"]["decision"] == "reject"
    assert srv.json(f"/api/jobs/{job['job_id']}/confirmation", method="POST", body={"decision": "approve"})[0] == 409
    assert srv.json(f"/api/jobs/{job['job_id']}/confirmation", method="POST", body={"decision": "maybe"})[0] == 422


def test_ui_confirmation_timeout_rejects_and_is_recorded(tmp_path):
    s = Server(tmp_path, confirm_timeout_s=0.3)
    try:
        job = _start(s, task_id="f_send_sms", policy="ui")
        evs = s.events(job["job_id"])
        resolved = next(e["data"] for e in evs if e["event"] == "confirmation_resolved")
        assert resolved == {"action": "send_sms", "decision": "reject", "by": "timeout"}
        run, _ = TraceStore(s.runs_dir).load_run(evs[-1]["data"]["run_ids"][0])
        assert run.meta["confirm_timeouts"] == [{"action": "send_sms", "timeout_s": 0.3}]
    finally:
        s.stop()


def test_non_f_fake_task_has_no_inserted_confirmation(srv):
    job = _start(srv, task_id="a_markor_note", policy="ui")
    evs = srv.events(job["job_id"])
    assert "confirmation" not in [e["event"] for e in evs]


# --- scoreboard ---------------------------------------------------------------------------


def test_scoreboard_reflects_runs(srv):
    status, before = srv.json("/api/scoreboard")
    assert before["fake"]["totals"]["runs"] == 0
    job = _start(srv, task_id="a_markor_note", policy="approve", repeat=2)
    srv.events(job["job_id"])
    status, board = srv.json("/api/scoreboard")
    fake = board["fake"]
    assert board["real"]["totals"]["runs"] == 0
    flow_a = next(r for r in fake["by_flow"] if r["flow_type"] == "a")
    assert flow_a["runs"] == 2 and flow_a["verifier_pass"] == {"rate": 0.0, "n": 2}
    assert flow_a["agent_said_done"] == {"rate": 1.0, "n": 2}
    assert flow_a["false_success"] == {"count": 2, "n": 2}  # fake device changes nothing, model says done
    assert board["ledger_total_usd"] == 0.0
