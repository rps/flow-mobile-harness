import io
import json
import os
import subprocess
import sys
from pathlib import Path

import harness.runner as runner
from harness import cli
from harness.contracts import Config
from harness.trace.store import TraceStore

REPO = Path(__file__).resolve().parents[1]


def _main(tmp_path, *argv):
    out = io.StringIO()
    code = cli.main(list(argv), out=out, config=Config(runs_dir=str(tmp_path / "runs")))
    return code, out.getvalue(), TraceStore(tmp_path / "runs")


def test_fake_task_run_writes_a_full_run_folder(tmp_path):
    code, out, store = _main(tmp_path, "run", "--task", "a_markor_note", "--fake", "--confirm", "approve")
    assert code == 0
    (run_id,) = store.list_runs()
    run, steps = store.load_run(run_id)
    assert (store.run_dir(run_id) / "replay.html").is_file()
    assert run.model == "fake-model" and run.meta["fake"] is True
    assert len(steps) == run.meta["steps"] == 5
    assert f"run_id={run_id}" in out and "verifier=FAIL" in out and "steps=5" in out
    assert not (tmp_path / "runs" / "ledger.json").exists()  # no spend recorded


def test_repeat_prints_summary_table(tmp_path):
    code, out, store = _main(tmp_path, "run", "--task", "a_markor_note", "--fake", "--repeat", "2",
                             "--confirm", "reject")
    assert code == 0 and len(store.list_runs()) == 2
    assert "2 runs, verifier 0/2 passed" in out
    assert out.count("a_markor_note") >= 4  # two run lines + two table rows


def test_all_runs_every_task(tmp_path):
    code, out, store = _main(tmp_path, "run", "--all", "--fake", "--confirm", "approve", "--seed", "5")
    runs = [store.load_run(r)[0] for r in store.list_runs()]
    tasks = {r.task_id for r in runs}
    assert [r.meta["steps"] for r in runs] == [5, 5, 5]  # each run gets a fresh scripted model
    assert code == 0 and tasks == {"a_markor_note", "b_contact_to_note", "f_send_sms"}
    assert "3 runs" in out


def test_freeform_refused_then_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASELINE_JSON", tmp_path / "missing.json")
    code, _, store = _main(tmp_path, "run", "--goal", "open Markor", "--fake", "--confirm", "approve")
    assert code == 2 and store.list_runs() == []
    code, out, store = _main(tmp_path, "run", "--goal", "open Markor", "--fake", "--confirm", "approve",
                             "--allow-unblocked")
    assert code == 0 and "verifier=unverified" in out and "task=freeform" in out


def test_unknown_task_and_bad_repeat_are_usage_errors(tmp_path):
    assert _main(tmp_path, "run", "--task", "nope", "--fake")[0] == 2
    assert _main(tmp_path, "run", "--task", "a_markor_note", "--fake", "--repeat", "0")[0] == 2
    assert not (tmp_path / "runs").exists() or TraceStore(tmp_path / "runs").list_runs() == []


def test_tasks_lists_registry(tmp_path):
    code, out, _ = _main(tmp_path, "tasks")
    assert code == 0
    assert [line.split()[0] for line in out.splitlines()] == ["a_markor_note", "b_contact_to_note", "f_send_sms"]


def test_replay_and_ledger(tmp_path):
    _main(tmp_path, "run", "--task", "a_markor_note", "--fake", "--confirm", "approve")
    (run_id,) = TraceStore(tmp_path / "runs").list_runs()
    html = TraceStore(tmp_path / "runs").run_dir(run_id) / "replay.html"
    html.unlink()
    code, out, _ = _main(tmp_path, "replay", run_id)
    assert code == 0 and html.is_file() and str(html) in out
    (tmp_path / "runs" / "ledger.json").write_text(json.dumps({"total_usd": 0.25, "entries": [{}, {}]}))
    code, out, _ = _main(tmp_path, "ledger")
    assert code == 0 and "total $0.2500 over 2 model calls" in out


def test_module_entry_point_end_to_end_with_prompt_and_no_tty(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO), "HARNESS_RUNS_DIR": str(tmp_path / "runs")}
    proc = subprocess.run(
        [sys.executable, "-m", "harness.cli", "run", "--task", "a_markor_note", "--fake"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "confirmation: prompt -> reject (stdin is not a tty)" in proc.stdout
    store = TraceStore(tmp_path / "runs")
    (run_id,) = store.list_runs()
    run, _ = store.load_run(run_id)
    assert run.meta["confirm_policy"] == "prompt" and run.meta["confirm_effective"] == "reject"
    assert (store.run_dir(run_id) / "replay.html").is_file()
