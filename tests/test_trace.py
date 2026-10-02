import io
import json
import re
from html.parser import HTMLParser

import pytest
from PIL import Image

import harness.contracts as c
from harness.trace.replay import render_replay
from harness.trace.store import TraceStore, new_run_id


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 16), color).save(buf, format="PNG")
    return buf.getvalue()


def _run(run_id="20261002-120000-abc123", **kw):
    base = dict(
        run_id=run_id,
        task_id="markor_create_note",
        flow_type=c.FlowType.A,
        goal="Create a note called <b>groceries</b>",
        model="claude-opus-5-5",
        started_at="2026-10-02T12:00:00+00:00",
    )
    base.update(kw)
    return c.RunRecord(**base)


def _finished(run):
    run.ended_at = "2026-10-02T12:01:30+00:00"
    run.agent_verdict = c.Verdict.DONE
    run.agent_summary = "Created the note groceries"
    run.termination_reason = c.TerminationReason.FINISHED
    run.usage = c.TokenUsage(1200, 300, 50, 10)
    run.estimated_cost_usd = 0.0421
    run.seed = 7
    run.verifier_result = c.VerifierResult(
        passed=False,
        oracle_tier=c.OracleTier.OWN_STORAGE,
        end_state=[c.CheckResult("note_exists", True, "found groceries.md")],
        side_effects=[c.CheckResult("no_other_files_changed", True)],
        process=[c.CheckResult("used_markor_ui", False, "used adb shell")],
        self_report_agrees=False,
    )
    return run


def _step(i, tool="tap"):
    return c.StepRecord(
        index=i,
        started_at=f"2026-10-02T12:00:0{i}+00:00",
        ended_at=f"2026-10-02T12:00:0{i + 1}+00:00",
        tool_name=tool,
        tool_input={"x": 10 * i, "y": 20},
        tool_result={"ok": True},
        reasoning=f"reason for step {i}",
        usage=c.TokenUsage(100, 20),
        duration_ms=900 + i,
    )


def _write_three(store, run):
    w = store.start_run(run)
    stored = []
    for i, (tool, color) in enumerate([("open_app", "red"), ("tap", "green"), ("type_text", "blue")]):
        stored.append(w.write_step(_step(i, tool), _png(color), f"tree {i}"))
    return w, stored


def test_new_run_id_format_and_unique():
    a, b = new_run_id(), new_run_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", a)
    assert a != b


def test_start_run_writes_run_json_and_refuses_existing(tmp_path):
    store = TraceStore(tmp_path / "runs")
    run = _run()
    store.start_run(run)
    run_dir = tmp_path / "runs" / run.run_id
    assert json.loads((run_dir / "run.json").read_text())["goal"] == run.goal
    with pytest.raises(FileExistsError):
        store.start_run(run)


def test_start_run_allows_existing_folder_without_run_json(tmp_path):
    (tmp_path / "r1").mkdir()
    TraceStore(tmp_path).start_run(_run("r1"))
    assert (tmp_path / "r1" / "run.json").is_file()


def test_write_step_saves_files_with_relative_paths(tmp_path):
    store = TraceStore(tmp_path)
    run = _run()
    _, stored = _write_three(store, run)
    run_dir = tmp_path / run.run_id
    assert [s.screenshot_path for s in stored] == ["step_000.png", "step_001.png", "step_002.png"]
    assert [s.ui_tree_path for s in stored] == ["step_000.txt", "step_001.txt", "step_002.txt"]
    assert (run_dir / "step_001.png").read_bytes() == _png("green")
    assert (run_dir / "step_002.txt").read_text() == "tree 2"
    assert len((run_dir / "steps.jsonl").read_text().splitlines()) == 3


def test_write_step_without_files_keeps_caller_paths(tmp_path):
    w = TraceStore(tmp_path).start_run(_run())
    step = _step(0)
    step.screenshot_path = "elsewhere.png"
    stored = w.write_step(step, None, None)
    assert stored.screenshot_path == "elsewhere.png"
    assert stored.ui_tree_path is None
    assert not (tmp_path / _run().run_id / "step_000.png").exists()


def test_round_trip(tmp_path):
    store = TraceStore(tmp_path)
    run = _run()
    w, stored = _write_three(store, run)
    w.finish(_finished(run))
    loaded_run, loaded_steps = store.load_run(run.run_id)
    assert loaded_run == run
    assert loaded_steps == stored
    assert store.last_skipped_lines == 0
    assert not list((tmp_path / run.run_id).glob("*.tmp"))


def test_partial_trace_without_finish_loads(tmp_path):
    store = TraceStore(tmp_path)
    run = _run()
    _write_three(store, run)
    loaded_run, steps = store.load_run(run.run_id)
    assert loaded_run.ended_at is None and loaded_run.termination_reason is None
    assert [s.index for s in steps] == [0, 1, 2]


def test_corrupt_lines_are_skipped_and_counted(tmp_path, caplog):
    store = TraceStore(tmp_path)
    run = _run()
    w = store.start_run(run)
    w.write_step(_step(0), None, None)
    with open(tmp_path / run.run_id / "steps.jsonl", "a") as f:
        f.write("{not json}\n")
        f.write('{"index": 9}\n')  # valid JSON, missing fields
    w.write_step(_step(1), None, None)
    with open(tmp_path / run.run_id / "steps.jsonl", "a") as f:
        f.write('{"index": 2, "started_')  # crash mid-write
    _, steps = store.load_run(run.run_id)
    assert [s.index for s in steps] == [0, 1]
    assert store.last_skipped_lines == 3
    assert "skipped" in caplog.text


def test_missing_steps_file_gives_empty_list(tmp_path):
    store = TraceStore(tmp_path)
    store.start_run(_run())
    assert store.load_run(_run().run_id)[1] == []


def test_load_missing_run_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        TraceStore(tmp_path).load_run("nope")


def test_list_runs(tmp_path):
    store = TraceStore(tmp_path / "runs")
    assert store.list_runs() == []
    store.start_run(_run("20261002-120000-bbbbbb"))
    store.start_run(_run("20261001-120000-aaaaaa"))
    (tmp_path / "runs" / "stray").mkdir()
    assert store.list_runs() == ["20261001-120000-aaaaaa", "20261002-120000-bbbbbb"]


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


def _parse(path):
    p = _Collect()
    p.feed(path.read_text())
    p.close()
    return p


def test_render_replay_full_run(tmp_path):
    store = TraceStore(tmp_path)
    run = _run()
    w, _ = _write_three(store, run)
    w.finish(_finished(run))
    out = render_replay(run.run_id, runs_dir=tmp_path)
    assert out == tmp_path / run.run_id / "replay.html"
    html = out.read_text()
    p = _parse(out)
    text = " ".join(p.text)

    assert "script" not in p.tags
    assert "link" not in p.tags
    for name, value in p.attrs:
        if name in ("src", "href"):
            assert value.startswith("data:image/png;base64,")
    assert sum(1 for t in p.tags if t == "img") == 3
    assert ("name", "viewport") in p.attrs
    assert "prefers-color-scheme" in html

    assert "<b>groceries</b>" not in html
    assert "Create a note called <b>groceries</b>" in text  # escaped, shown as text
    for expected in [
        "markor_create_note", "claude-opus-5-5", "90.0 s", "1200 in", "$0.0421",
        "open_app", "tap", "type_text", "reason for step 2", "902 ms",
        "done", "Created the note groceries", "finished",
        "note_exists", "found groceries.md", "no_other_files_changed",
        "used_markor_ui", "used adb shell", "1 (own_storage)",
    ]:
        assert expected in text, expected


def test_render_replay_freeform_unfinished_missing_screenshot(tmp_path):
    store = TraceStore(tmp_path)
    run = _run(task_id=None, flow_type=c.FlowType.FREEFORM)
    w = store.start_run(run)
    w.write_step(_step(0), _png("red"), None)
    (tmp_path / run.run_id / "step_000.png").unlink()
    step = _step(1)
    step.screenshot_path = "../../outside.png"
    w.write_step(step, None, None)
    text = " ".join(_parse(render_replay(run.run_id, runs_dir=tmp_path)).text)
    for placeholder in ["screenshot not recorded", "no verifier (freeform)", "unknown",
                        "unfinished", "none (harness ended the run)"]:
        assert placeholder in text, placeholder
