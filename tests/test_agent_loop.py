import base64
import json
from pathlib import Path

import anthropic
import httpx2
import pytest

from harness.agent import prompts
from harness.agent.loop import AgentSettings, run_agent
from harness.agent.pricing import Ledger
from harness.contracts import Config, ConfirmationDecision, TerminationReason, Verdict
from tests.agent_fakes import FakeDevice, ScriptedModel, text_only, tool


@pytest.fixture
def config(tmp_path):
    return Config(runs_dir=str(tmp_path), max_steps=10, budget_usd=2.0)


class Recorder:
    def __init__(self):
        self.steps = []

    def __call__(self, record, png, tree):
        self.steps.append((record, png, tree))


def approve(_req):
    return ConfirmationDecision.APPROVE


def _images(request):
    """All image blocks anywhere in a request's messages."""
    found = []

    def walk(blocks):
        for b in blocks if isinstance(blocks, list) else []:
            if b.get("type") == "image":
                found.append(b)
            walk(b.get("content"))

    for m in request["messages"]:
        walk(m["content"])
    return found


def _texts(request):
    out = []

    def walk(blocks):
        for b in blocks if isinstance(blocks, list) else []:
            if b.get("type") == "text":
                out.append(b["text"])
            walk(b.get("content"))

    for m in request["messages"]:
        walk(m["content"])
    return out


def test_three_step_success(config):
    device = FakeDevice()
    model = ScriptedModel([
        tool("tap", _text="Open the compose box", _thinking="the button is top right", x=10, y=20),
        tool("type_text", _usage=(200, 30, 50, 10), text="hello"),
        tool("finish", verdict="done", summary="Note saved; it is visible in the list."),
    ])
    rec = Recorder()
    out = run_agent("Write hello in a new note", device, config, approve, rec, model_client=model)

    assert out.termination_reason is TerminationReason.FINISHED
    assert out.verdict is Verdict.DONE
    assert out.summary == "Note saved; it is visible in the list."
    assert out.steps == 3
    assert device.calls == [("tap", 10, 20), ("type_text", "hello")]

    # usage summed over three responses; cost from the opus-5-5 table
    assert (out.usage.input_tokens, out.usage.output_tokens) == (400, 70)
    assert (out.usage.cache_read_input_tokens, out.usage.cache_creation_input_tokens) == (50, 10)
    expected = (400 * 4.0 + 70 * 20.0 + 50 * 0.2 + 10 * 5.0) / 1e6
    assert out.estimated_cost_usd == pytest.approx(expected)
    assert Ledger(config.runs_dir + "/ledger.json").total() == pytest.approx(expected)

    # on_step: the screenshot the model saw before each action
    assert [r.tool_name for r, _, _ in rec.steps] == ["tap", "type_text", "finish"]
    assert [png for _, png, _ in rec.steps] == [b"PNG-1", b"PNG-2", b"PNG-3"]
    assert rec.steps[0][2] == "<node text='frame 1'/>"
    first = rec.steps[0][0]
    assert first.index == 0 and first.tool_input == {"x": 10, "y": 20} and first.tool_result == {"ok": True}
    assert "the button is top right" in first.reasoning and "Open the compose box" in first.reasoning
    assert first.usage.input_tokens == 100

    # first request: goal + screenshot + tree only; tool_choice auto, one call
    req0 = model.requests[0]
    assert req0["model"] == "claude-opus-5-5"
    assert req0["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert [t["name"] for t in req0["tools"]] == [
        "tap", "type_text", "swipe", "back", "home", "open_app",
        "query_structured", "request_confirmation", "finish",
    ]
    user0 = req0["messages"]
    assert len(user0) == 1
    blocks = user0[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Goal: Write hello in a new note"}
    assert base64.b64decode(blocks[1]["source"]["data"]) == b"PNG-1"
    assert blocks[2]["text"].endswith("<node text='frame 1'/>")
    assert len(blocks) == 3

    # second request: tool_result carries the result, new screenshot and tree
    result_msg = model.requests[1]["messages"][-1]
    tr = result_msg["content"][0]
    assert tr["type"] == "tool_result" and tr["is_error"] is False
    assert json.loads(tr["content"][0]["text"]) == {"ok": True}
    assert base64.b64decode(tr["content"][1]["source"]["data"]) == b"PNG-2"
    # thinking is never replayed
    assistant = model.requests[1]["messages"][1]
    assert [b["type"] for b in assistant["content"]] == ["text", "tool_use"]


def test_confirmation_reject_then_infeasible(config):
    device = FakeDevice()
    model = ScriptedModel([
        tool("request_confirmation", action="Send message", summary={"to": "Ann", "text": "hi"}),
        tool("finish", verdict="infeasible", summary="User rejected sending."),
    ])
    asked = []

    def reject(req):
        asked.append(req)
        return ConfirmationDecision.REJECT

    rec = Recorder()
    out = run_agent("Message Ann", device, config, reject, rec, model_client=model)

    assert out.verdict is Verdict.INFEASIBLE and out.termination_reason is TerminationReason.FINISHED
    assert [(r.action, r.summary) for r in asked] == [("Send message", {"to": "Ann", "text": "hi"})]
    step = rec.steps[0][0]
    assert step.tool_name == "request_confirmation"
    assert step.tool_input == {"action": "Send message", "summary": {"to": "Ann", "text": "hi"}}
    assert step.tool_result == {"decision": "reject"}
    tr = model.requests[1]["messages"][-1]["content"][0]
    assert json.loads(tr["content"][0]["text"]) == {"decision": "reject"}
    assert tr["content"][1]["text"] == prompts.REJECT_NOTE
    assert device.calls == []


def test_confirmation_approve_record_shape(config):
    model = ScriptedModel([
        tool("request_confirmation", action="Pay", summary={"amount": 5}),
        tool("tap", x=1, y=2),
        tool("finish", verdict="done", summary="Paid."),
    ])
    rec = Recorder()
    out = run_agent("Pay 5", FakeDevice(), config, approve, rec, model_client=model)
    assert out.verdict is Verdict.DONE
    assert rec.steps[0][0].tool_result == {"decision": "approve"}
    tr = model.requests[1]["messages"][-1]["content"][0]
    assert prompts.REJECT_NOTE not in [b.get("text") for b in tr["content"]]


def test_repeated_rerequest_after_reject_fails(config):
    ask = tool("request_confirmation", action="Delete  note", summary={"name": "a"})
    model = ScriptedModel([ask], repeat_last=True)
    asked = []

    def reject(req):
        asked.append(req)
        return ConfirmationDecision.REJECT

    rec = Recorder()
    out = run_agent("Delete note a", FakeDevice(), config, reject, rec, model_client=model)

    assert out.verdict is Verdict.FAILED
    assert out.termination_reason is TerminationReason.FINISHED
    assert "harness stopped" in out.summary
    # first request + 2 re-requests reach the handler; the 3rd re-request does not
    assert len(asked) == 3
    assert out.steps == 3 and len(rec.steps) == 3


def test_step_cap(config):
    config.max_steps = 4
    device = FakeDevice()
    model = ScriptedModel([tool("swipe", x1=1, y1=900, x2=1, y2=100)], repeat_last=True)
    out = run_agent("Scroll forever", device, config, approve, Recorder(), model_client=model)
    assert out.termination_reason is TerminationReason.STEP_CAP
    assert out.verdict is None
    assert out.steps == 4 and len(device.calls) == 4 and len(model.requests) == 4


def test_budget_refusal_before_start(config):
    Ledger(config.runs_dir + "/ledger.json").add(1.8)
    device = FakeDevice()
    model = ScriptedModel([])
    out = run_agent("Anything", device, config, approve, Recorder(), model_client=model)
    assert out.termination_reason is TerminationReason.BUDGET
    assert out.steps == 0 and model.requests == [] and device.calls == [] and device.frame == 0
    assert out.estimated_cost_usd == 0.0


def test_per_run_cap_stops_mid_run(config):
    # each response costs 10_000 * $20 / 1M = $0.20 of output
    pricey = (0, 10_000, 0, 0)
    model = ScriptedModel([tool("back", _usage=pricey)], repeat_last=True)
    device = FakeDevice()
    settings = AgentSettings(per_run_cap_usd=0.5)
    out = run_agent("Loop", device, config, approve, Recorder(), model_client=model, settings=settings)
    assert out.termination_reason is TerminationReason.BUDGET
    # $0.20, $0.40 execute; the 3rd response pushes to $0.60 and is not executed
    assert len(model.requests) == 3 and out.steps == 2 and len(device.calls) == 2
    assert out.estimated_cost_usd == pytest.approx(0.6)
    assert Ledger(config.runs_dir + "/ledger.json").total() == pytest.approx(0.6)


def test_unpriced_model_is_refused(config):
    config.model = "claude-unknown-9"
    model = ScriptedModel([])
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model)
    assert out.termination_reason is TerminationReason.BUDGET and model.requests == []


def test_unpriced_model_allowed_reports_no_cost(config):
    config.model = "claude-unknown-9"
    model = ScriptedModel([tool("finish", verdict="done", summary="ok")])
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model,
                    settings=AgentSettings(allow_unpriced=True))
    assert out.verdict is Verdict.DONE and out.estimated_cost_usd is None
    assert not (Path(config.runs_dir) / "ledger.json").exists()


def test_history_pruning_keeps_latest_screenshots(config):
    model = ScriptedModel(
        [tool("tap", _thinking="t", x=i, y=i) for i in range(5)]
        + [tool("finish", verdict="failed", summary="gave up")]
    )
    run_agent("Prune", FakeDevice(), config, approve, Recorder(), model_client=model,
              settings=AgentSettings(keep_screenshots=3))
    last = model.requests[-1]
    imgs = _images(last)
    assert [base64.b64decode(i["source"]["data"]) for i in imgs] == [b"PNG-4", b"PNG-5", b"PNG-6"]
    texts = _texts(last)
    for step in (0, 1, 2):
        assert prompts.SCREENSHOT_OMITTED.format(step=step) in texts
    assert "thinking" not in json.dumps(last["messages"])

    # once pruned, the prefix before the image window is byte-identical next turn
    prev, cur = model.requests[-2], model.requests[-1]
    strip = lambda ms: json.dumps([{**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]]} for m in ms])
    n = 4  # messages 0..3 lie wholly before prev's image window
    assert strip(prev["messages"][:n]) == strip(cur["messages"][:n])
    # a breakpoint sits on the stable prefix, and the system prompt is cached
    marked = [i for i, m in enumerate(cur["messages"]) if any("cache_control" in b for b in m["content"])]
    assert len(marked) == 1 and marked[0] < len(cur["messages"]) - 1
    assert cur["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_no_tool_call_is_nudged_then_errors(config):
    model = ScriptedModel([text_only("thinking about it")], repeat_last=True)
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model,
                    settings=AgentSettings(no_tool_call_retries=2))
    assert out.termination_reason is TerminationReason.ERROR and out.steps == 0
    assert len(model.requests) == 3
    assert model.requests[1]["messages"][-1]["content"][0]["text"] == prompts.NUDGE


def test_nudge_recovers(config):
    model = ScriptedModel([text_only("hmm"), tool("finish", verdict="done", summary="ok")])
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model)
    assert out.verdict is Verdict.DONE and out.steps == 1


def test_api_error_ends_with_error(config):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    model = ScriptedModel([tool("tap", x=1, y=1), err])
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model)
    assert out.termination_reason is TerminationReason.ERROR and out.steps == 1
    assert "APIConnectionError" in out.summary


def test_refusal_ends_with_error(config):
    refused = text_only("")
    refused = refused.model_copy(update={"stop_reason": "refusal", "content": []})
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=ScriptedModel([refused]))
    assert out.termination_reason is TerminationReason.ERROR and "refused" in out.summary


def test_single_device_error_is_reported_and_run_continues(config):
    device = FakeDevice(queries={"contacts.list": {"rows": [], "total": 0}})
    model = ScriptedModel([
        tool("query_structured", name="contacts.list", params={"query": "Lars Duarte"}),
        tool("query_structured", name="contacts.list", params={}),
        tool("finish", verdict="done", summary="ok"),
    ])
    rec = Recorder()
    out = run_agent("x", device, config, approve, rec, model_client=model)

    assert out.termination_reason is TerminationReason.FINISHED and out.verdict is Verdict.DONE
    # not retried: the bad query ran once, then the model's corrected query
    assert [c[2] for c in device.calls] == [{"query": "Lars Duarte"}, {}]
    assert rec.steps[0][0].tool_result == {"error": "query_structured failed: unknown parameters: query"}
    err = model.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and all(b["type"] == "text" for b in err["content"])
    assert "unknown parameters: query" in err["content"][0]["text"]
    # no new screenshot after the error turn
    assert [png for _, png, _ in rec.steps] == [b"PNG-1", b"PNG-1", b"PNG-2"]


def test_three_consecutive_device_errors_end_the_run(config):
    device = FakeDevice(fail_actions=10)
    model = ScriptedModel([tool("home")], repeat_last=True)
    rec = Recorder()
    out = run_agent("x", device, config, approve, rec, model_client=model)
    assert out.termination_reason is TerminationReason.ERROR and out.verdict is None
    assert "3 device errors in a row" in out.summary and "home failed" in out.summary
    assert out.steps == 3 and len(rec.steps) == 3 and len(device.calls) == 3


def test_device_error_counter_resets_on_success(config):
    device = FakeDevice(fail_actions=2)
    model = ScriptedModel([
        tool("home"), tool("home"),  # fail, fail
        tool("back"),  # succeeds, resets the counter
        tool("home"), tool("home"),  # fail, fail again
        tool("finish", verdict="done", summary="ok"),
    ])

    def fail_two_more(record, png, tree):
        if record.tool_name == "back":
            device.fail_actions = 2

    out = run_agent("x", device, config, approve, fail_two_more, model_client=model)
    assert out.verdict is Verdict.DONE and out.steps == 6


def test_observation_errors_are_retried(config):
    device = FakeDevice(fail_screenshots=1)
    model = ScriptedModel([tool("finish", verdict="done", summary="ok")])
    out = run_agent("x", device, config, approve, Recorder(), model_client=model)
    assert out.verdict is Verdict.DONE and device.frame == 1


def test_query_tool_description_names_device_queries(config):
    device = FakeDevice(queries={"sms.list": {}, "contacts.list": {}})
    model = ScriptedModel([tool("finish", verdict="done", summary="ok")])
    run_agent("x", device, config, approve, Recorder(), model_client=model)
    (q,) = [t for t in model.requests[0]["tools"] if t["name"] == "query_structured"]
    d = q["description"]
    assert "The only query names are: contacts.list, sms.list." in d
    assert '"limit" (1..200, default 50)' in d and "no search or filter" in d
    assert q["input_schema"]["properties"]["params"]["properties"] == {
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}
    }


def test_query_tool_description_falls_back_to_defaults(config):
    class NoList(FakeDevice):
        def allowed_queries(self):
            raise RuntimeError("not available")

    model = ScriptedModel([tool("finish", verdict="done", summary="ok")])
    run_agent("x", NoList(), config, approve, Recorder(), model_client=model)
    (q,) = [t for t in model.requests[0]["tools"] if t["name"] == "query_structured"]
    assert "calendar.events, contacts.list, sms.list" in q["description"]


def test_query_not_allowed_goes_back_to_model(config):
    device = FakeDevice(queries={"notes.list": {"notes": ["a"]}})
    model = ScriptedModel([
        tool("query_structured", name="contacts.dump", params={}),
        tool("query_structured", name="notes.list", params={}),
        tool("finish", verdict="done", summary="ok"),
    ])
    rec = Recorder()
    out = run_agent("x", device, config, approve, rec, model_client=model)
    assert out.verdict is Verdict.DONE
    tr = model.requests[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True
    assert json.loads(tr["content"][0]["text"])["allowed_queries"] == ["notes.list"]
    assert rec.steps[1][0].tool_result == {"ok": True, "result": {"notes": ["a"]}}


def test_invalid_input_returned_as_error(config):
    model = ScriptedModel([
        tool("tap", x="ten", y=1),
        tool("finish", verdict="maybe", summary="?"),
        tool("finish", verdict="done", summary="ok"),
    ])
    device = FakeDevice()
    out = run_agent("x", device, config, approve, Recorder(), model_client=model)
    assert out.verdict is Verdict.DONE and device.calls == []
    errs = [json.loads(r["messages"][-1]["content"][0]["content"][0]["text"]) for r in model.requests[1:]]
    assert "x" in errs[0]["error"] and "verdict" in errs[1]["error"]


def test_timeout(config):
    config.wall_clock_s = 10
    now = [0.0]

    def clock():
        now[0] += 4.0
        return now[0]

    model = ScriptedModel([tool("back")], repeat_last=True)
    out = run_agent("x", FakeDevice(), config, approve, Recorder(), model_client=model, clock=clock)
    assert out.termination_reason is TerminationReason.TIMEOUT
    assert out.steps < config.max_steps


def test_on_step_failure_is_error(config):
    def broken(*_):
        raise OSError("disk full")

    model = ScriptedModel([tool("back")])
    out = run_agent("x", FakeDevice(), config, approve, broken, model_client=model)
    assert out.termination_reason is TerminationReason.ERROR and "disk full" in out.summary


def test_system_prompt_rules():
    p = prompts.SYSTEM_PROMPT
    for phrase in ("Android phone", "request_confirmation", "infeasible", "cannot see on screen", "one tool call"):
        assert phrase in p


def _sensitive_rule() -> tuple[dict[str, str], str]:
    """The confirmation paragraph split into {category: bullet text} and the
    flattened prose that follows the bullets."""
    rule = next((par for par in prompts.SYSTEM_PROMPT.split("\n\n") if "request_confirmation" in par), None)
    assert rule is not None, "no paragraph mentions request_confirmation"
    lines = rule.splitlines()
    bullets = {ln[2:].split(":", 1)[0]: ln for ln in lines if ln.startswith("- ")}
    prose = " ".join(" ".join(ln for ln in lines if not ln.startswith("- ")).split())
    return bullets, prose


def test_sensitive_action_rule_covers_each_category():
    """Drafts count as financial; messages are outbound; deletions and
    settings are destructive. The old wording that let a draft invoice look
    exempt is gone."""
    bullets, prose = _sensitive_rule()
    expected = {
        "anything financial": ("invoice", "even as a draft"),
        "anything outbound": ("message",),
        "anything destructive or hard to undo": ("deleting", "settings"),
        "anything that commits another person or account.": (),
    }
    assert set(bullets) >= set(expected), bullets
    for category, phrases in expected.items():
        for phrase in phrases:
            assert phrase in bullets[category], (category, phrase)
    exemption = next((s for s in prose.split(". ") if "not sensitive" in s), "")
    for phrase in ("Reading", "navigating", "private note"):
        assert phrase in exemption, phrase
    assert "Only take the step if the answer is approve" in prose
    assert "do not ask again for the same action" in prose
    assert "cannot easily be undone" not in prompts.SYSTEM_PROMPT


def test_sensitive_action_rule_names_no_task():
    """The rule is general: no task id or per-task sensitive action appears
    anywhere in the system prompt or the tool definitions."""
    from harness.tasks import registry

    tasks = registry.all_tasks()
    assert any(t.sensitive_actions for t in tasks), "registry has no task with sensitive actions to check"
    from harness.agent.tools import TOOL_DEFINITIONS

    confirm = next((d for d in TOOL_DEFINITIONS if d["name"] == "request_confirmation"), None)
    assert confirm is not None and "drafts" in confirm["description"]
    tool_text = json.dumps(TOOL_DEFINITIONS)
    for t in tasks:
        for text in (prompts.SYSTEM_PROMPT, tool_text):
            assert t.id not in text, t.id
            for action in t.sensitive_actions:
                assert action not in text, (t.id, action)


def test_error_tool_results_never_carry_images(config):
    from anthropic.types import ToolUseBlock

    from tests.agent_fakes import _message, check_error_results_text_only

    two_calls = _message(
        [
            ToolUseBlock(type="tool_use", id="toolu_a", name="tap", input={"x": 1, "y": 1}),
            ToolUseBlock(type="tool_use", id="toolu_b", name="back", input={}),
        ],
        "tool_use",
        (100, 20, 0, 0),
    )
    device = FakeDevice(queries={"notes.list": {}})
    model = ScriptedModel([
        tool("query_structured", name="contacts.dump", params={}),  # QueryNotAllowed
        tool("tap", x="bad", y=1),  # invalid input
        two_calls,  # extra tool_use gets an error result
        tool("finish", verdict="done", summary="ok"),
    ])
    rec = Recorder()
    out = run_agent("x", device, config, approve, rec, model_client=model)

    assert out.verdict is Verdict.DONE and out.steps == 4
    for request in model.requests:
        check_error_results_text_only(request)
        for msg in request["messages"]:
            for block in msg["content"]:
                if block.get("type") == "tool_result" and block["is_error"]:
                    assert all(b["type"] == "text" for b in block["content"]) if isinstance(block["content"], list) else True
    # error turns take no new screenshot: the model keeps seeing frame 1 until the tap runs
    assert [png for _, png, _ in rec.steps] == [b"PNG-1", b"PNG-1", b"PNG-1", b"PNG-2"]
    err = model.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and err["content"][-1]["text"] == prompts.SCREEN_UNCHANGED
    # the successful tap's result still carries the new screenshot, the extra call is text-only
    last = model.requests[3]["messages"][-1]["content"]
    assert last[0]["is_error"] is False and any(b["type"] == "image" for b in last[0]["content"])
    assert last[1] == {"type": "tool_result", "tool_use_id": "toolu_b", "content": prompts.ONE_ACTION_ONLY, "is_error": True}


def test_default_per_run_cap():
    assert AgentSettings().per_run_cap_usd == 1.50
