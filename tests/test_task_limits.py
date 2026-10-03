"""Per-task run limits: every task has them, and they replace the Config caps for that task only."""

import dataclasses

import pytest

from harness.agent.loop import AgentSettings
from harness.contracts import Config, RunLimits, TerminationReason
from harness.fake_env import FAKE_MODEL, FakeEmulator, MemoryInspector, ScriptedClient, make_fake_device
from harness.runner import _apply_task_limits, run_task
from harness.tasks import registry
from harness.tasks.limits import LIMITS
from harness.trace.store import TraceStore

SETTINGS = AgentSettings(allow_unpriced=True, run_reserve_usd=0.0)


def _run(tmp_path, task_id=None, goal=None, **config_kw):
    inspector = MemoryInspector()
    store = TraceStore(tmp_path / "runs")
    config = Config(model=FAKE_MODEL, runs_dir=str(store.runs_dir), **config_kw)
    run = run_task(task_id, goal, config, "approve", device_factory=make_fake_device,
                   inspector_factory=lambda: inspector, emulator=FakeEmulator(inspector), store=store,
                   model_client=ScriptedClient(), settings=SETTINGS, allow_unblocked=True, meta={"fake": True})
    return store.load_run(run.run_id)[0]


def test_every_registered_task_has_limits_and_the_table_names_no_other_task():
    assert {t.id for t in registry.all_tasks()} == set(LIMITS)
    for task in registry.all_tasks():
        assert task.limits is LIMITS[task.id]
        assert task.limits.max_steps > 0 and task.limits.wall_clock_s > 0 and task.limits.run_cap_usd > 0


def test_longer_tasks_get_more_headroom_than_the_short_ones():
    short, long = LIMITS["f_send_sms"], LIMITS["h2_note_to_order_infeasible"]
    assert long.max_steps > short.max_steps and long.wall_clock_s > short.wall_clock_s
    assert long.run_cap_usd > short.run_cap_usd
    # the two note-to-order tasks stopped at the old global 40 steps; 51 was the longest passing run
    assert LIMITS["b2_note_to_order"].max_steps >= 60 and long.max_steps > 51


def test_a_tight_task_limit_stops_the_run_even_when_config_allows_more(tmp_path, monkeypatch):
    monkeypatch.setattr(registry.get("a_markor_note"), "limits", RunLimits(2, 600.0, 1.0))
    run = _run(tmp_path, "a_markor_note", max_steps=40)
    assert run.termination_reason == TerminationReason.STEP_CAP
    assert run.meta["steps"] == 2
    assert run.meta["limits"] == {"max_steps": 2, "wall_clock_s": 600.0, "run_cap_usd": 1.0}


def test_a_task_limit_gives_headroom_over_a_tighter_config(tmp_path):
    run = _run(tmp_path, "a_markor_note", max_steps=2)  # the scripted run needs 5 steps; the task allows 30
    assert run.termination_reason == TerminationReason.FINISHED and run.meta["steps"] == 5


def test_freeform_goals_keep_the_config_limits(tmp_path):
    run = _run(tmp_path, goal="Open Markor", max_steps=2)
    assert run.termination_reason == TerminationReason.STEP_CAP and run.meta["steps"] == 2
    assert "limits" not in run.meta


def test_a_task_without_limits_keeps_the_config_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(registry.get("a_markor_note"), "limits", None)
    run = _run(tmp_path, "a_markor_note", max_steps=2)
    assert run.termination_reason == TerminationReason.STEP_CAP and "limits" not in run.meta


def test_the_spend_cap_reaches_both_config_and_explicit_settings():
    limits = RunLimits(60, 1200.0, 2.5)
    base = Config(max_steps=40, wall_clock_s=600.0, run_cap_usd=1.5, budget_usd=9.0)
    config, settings = _apply_task_limits(limits, base, AgentSettings(effort="high"))
    assert (config.max_steps, config.wall_clock_s, config.run_cap_usd) == (60, 1200.0, 2.5)
    assert config.budget_usd == 9.0  # the overall budget is not a per-task limit
    assert settings.per_run_cap_usd == 2.5 and settings.effort == "high"
    config, settings = _apply_task_limits(limits, base, None)
    assert settings is None and config.run_cap_usd == 2.5  # run_agent builds settings from Config


def test_a_run_capped_above_the_remaining_budget_is_refused_before_any_model_call(tmp_path, monkeypatch):
    """The budget guard reserves the task's own cap, so a costly task cannot start on a nearly spent budget."""
    monkeypatch.setattr(registry.get("a_markor_note"), "limits", RunLimits(30, 600.0, 3.0))
    inspector = MemoryInspector()
    store = TraceStore(tmp_path / "runs")
    config = Config(model="claude-opus-5-5", runs_dir=str(store.runs_dir), budget_usd=2.0)
    client = ScriptedClient()
    run = run_task("a_markor_note", None, config, "approve", device_factory=make_fake_device,
                   inspector_factory=lambda: inspector, emulator=FakeEmulator(inspector), store=store,
                   model_client=client, settings=dataclasses.replace(SETTINGS, run_reserve_usd=None),
                   meta={"fake": True})
    assert run.termination_reason == TerminationReason.BUDGET and run.meta["steps"] == 0
    assert "reserve $3.0000" in run.agent_summary
