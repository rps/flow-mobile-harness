import ast
import json
import sys
from pathlib import Path

import pytest

import harness.contracts as c


def _json_round_trip(d):
    return json.loads(json.dumps(d))


def test_contracts_imports_stdlib_only():
    tree = ast.parse(Path(c.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots - set(sys.stdlib_module_names) - {"__future__"} == set()


def _run_record():
    verifier = c.VerifierResult(
        passed=False,
        oracle_tier=c.OracleTier.OWN_STORAGE,
        end_state=[c.CheckResult("note_exists", True, "found notes/a.md")],
        side_effects=[c.CheckResult("no_sms_sent", False, "1 new sms")],
        process=[c.CheckResult("confirmed_before_send", True)],
        self_report_agrees=False,
    )
    return c.RunRecord(
        run_id="r1",
        task_id="f_send_message",
        flow_type=c.FlowType.F,
        goal="Send Ana the meeting time",
        model="claude-opus-5-5",
        started_at="2026-10-02T18:00:00Z",
        ended_at="2026-10-02T18:02:00Z",
        agent_verdict=c.Verdict.DONE,
        agent_summary="Sent",
        termination_reason=c.TerminationReason.FINISHED,
        usage=c.TokenUsage(100, 20, 5, 1),
        estimated_cost_usd=0.12,
        verifier_result=verifier,
        seed=42,
    )


def test_run_record_round_trips_through_json_with_enum_values():
    run = _run_record()
    d = _json_round_trip(run.to_dict())
    assert d["flow_type"] == "f"
    assert d["agent_verdict"] == "done"
    assert d["termination_reason"] == "finished"
    assert d["verifier_result"]["oracle_tier"] == 1
    assert d["verifier_result"]["side_effects"][0]["passed"] is False
    assert c.RunRecord.from_dict(d) == run


def test_harness_terminated_freeform_run_round_trips_with_nones():
    run = c.RunRecord(
        run_id="r2", task_id=None, flow_type=c.FlowType.FREEFORM, goal="anything",
        model="m", started_at="t0", termination_reason=c.TerminationReason.STEP_CAP,
    )
    back = c.RunRecord.from_dict(_json_round_trip(run.to_dict()))
    assert back == run
    assert back.agent_verdict is None and back.verifier_result is None and back.seed is None


def test_step_record_round_trips():
    step = c.StepRecord(
        index=3, started_at="t0", ended_at="t1", tool_name="tap",
        tool_input={"x": 10, "y": 20}, tool_result={"ok": True},
        reasoning="tap Save", screenshot_path="screens/003.png",
        ui_tree_path="trees/003.txt", usage=c.TokenUsage(input_tokens=7), duration_ms=850,
    )
    assert c.StepRecord.from_dict(_json_round_trip(step.to_dict())) == step


def test_confirmation_request_round_trips():
    req = c.ConfirmationRequest("send_message", {"to": "Ana", "body": "3pm"})
    assert c.ConfirmationRequest.from_dict(_json_round_trip(req.to_dict())) == req


def test_from_dict_rejects_unknown_enum_value():
    d = _run_record().to_dict()
    d["agent_verdict"] = "maybe"
    with pytest.raises(ValueError):
        c.RunRecord.from_dict(d)


def test_token_usage_addition_and_oracle_headline():
    total = c.TokenUsage(1, 2, 3, 4) + c.TokenUsage(10, 20, 30, 40)
    assert total == c.TokenUsage(11, 22, 33, 44)
    assert [t.is_headline for t in c.OracleTier] == [True, True, True, False, False, False]


def test_load_env_file_parses_comments_quotes_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\n\nexport A="x y"\nB=\'q\'\nC=plain\nnot a pair\n')
    assert c.load_env_file(f) == {"A": "x y", "B": "q", "C": "plain"}
    assert c.load_env_file(tmp_path / "missing") == {}


def test_config_precedence_environ_over_file_over_defaults(tmp_path):
    f = tmp_path / ".env"
    f.write_text("HARNESS_MAX_STEPS=12\nHARNESS_MODEL=from-file\nANTHROPIC_API_KEY=sk-secret-sentinel\n")
    cfg = c.Config.from_env(f, environ={"HARNESS_MODEL": "from-env", "HARNESS_BUDGET_USD": "0.5"})
    assert cfg.model == "from-env"
    assert cfg.max_steps == 12
    assert cfg.budget_usd == 0.5
    assert cfg.wall_clock_s == 600.0 and cfg.screenshot_max_px == 1280
    assert "sk-secret-sentinel" not in repr(cfg)
    assert "sk-secret-sentinel" not in json.dumps(cfg.to_dict())
    assert cfg.require_api_key() == "sk-secret-sentinel"


def test_config_defaults_and_missing_key(tmp_path):
    cfg = c.Config.from_env(tmp_path / "none", environ={})
    assert cfg == c.Config()
    assert cfg.model == "claude-opus-5-5"
    with pytest.raises(c.ConfigError):
        cfg.require_api_key()


@pytest.mark.parametrize("env", [{"HARNESS_MAX_STEPS": "ten"}, {"HARNESS_WALL_CLOCK_S": "0"}])
def test_config_rejects_bad_values(env):
    with pytest.raises(c.ConfigError):
        c.Config.from_env(None, environ=env)


class _FakeDevice:
    def screenshot(self): ...
    def ui_tree(self): ...
    def tap(self, x, y): ...
    def type_text(self, text): ...
    def swipe(self, x1, y1, x2, y2, duration_ms=300): ...
    def back(self): ...
    def home(self): ...
    def open_app(self, package): ...
    def allowed_queries(self): ...
    def query_structured(self, name, params): ...


class _NoQueryDevice:
    def screenshot(self): ...
    def tap(self, x, y): ...


def test_device_protocol_structural_check():
    assert isinstance(_FakeDevice(), c.Device)
    assert not isinstance(_NoQueryDevice(), c.Device)


def test_screenshot_maps_scaled_to_device_pixels():
    shot = c.Screenshot(b"", width=1080, height=2400, scaled_width=540, scaled_height=1200)
    assert shot.to_device(270, 600) == (540, 1200)
    assert shot.to_device(0, 1200) == (0, 2400)
