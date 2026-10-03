import re

import pytest

from harness.contracts import FlowType, OracleTier
from harness.seed.generator import generate_plan
from harness.tasks import catalog
from harness.tasks.registry import all_tasks, get, render_goal

HINT_WORDS = ["verif", "check", "provider", "database", "/sdcard", "file", "adb", "seed",
              "confirm", "approv", "test", "expected"]


def test_registry_lists_and_gets_tasks():
    ids = [t.id for t in all_tasks()]
    assert ids == ["a_markor_note", "b_contact_to_note", "f_send_sms", "d_orders_to_note_to_message",
                   "f2_place_order", "c_variant_b", "e_provider_full", "e_provider_fallback",
                   "drift_provider_vs_ui", "h_cancel_order", "b2_note_to_order", "h2_note_to_order_infeasible",
                   "biz_b1_hours", "biz_b2_rate", "biz_d_invoice", "biz_h"]
    assert get("f_send_sms").flow_type == FlowType.F
    with pytest.raises(KeyError):
        get("nope")


def test_oracle_tiers_and_sensitive_actions():
    assert get("a_markor_note").oracle_tier == OracleTier.OWN_STORAGE
    assert get("b_contact_to_note").oracle_tier == OracleTier.OWN_STORAGE
    assert get("f_send_sms").oracle_tier == OracleTier.DOWNSTREAM_EFFECT
    assert get("f_send_sms").sensitive_actions == ["send_sms"]
    assert all(t.oracle_tier.is_headline for t in all_tasks() if not t.id.startswith("biz_"))


@pytest.mark.parametrize("task", all_tasks(), ids=lambda t: t.id)
def test_goal_has_no_hints_and_renders_fully(task):
    for word in HINT_WORDS:
        assert word not in task.goal.lower(), word
    for seed in range(10):
        plan = generate_plan(seed, task)
        goal = render_goal(task, plan)
        assert not re.search(r"[{}]", goal)
        for word in HINT_WORDS:
            assert word not in goal.lower(), word


def test_b_and_f_goals_do_not_reveal_the_values_to_find():
    for seed in range(10):
        plan = generate_plan(seed, get("b_contact_to_note"))
        goal = render_goal(get("b_contact_to_note"), plan)
        assert plan.expected["name"] in goal
        assert plan.expected["phone"] not in goal and plan.expected["email"] not in goal
        plan = generate_plan(seed, get("f_send_sms"))
        goal = render_goal(get("f_send_sms"), plan)
        assert plan.expected["message"] in goal and plan.expected["phone"] not in goal


def test_catalog_names_every_task_and_flow():
    assert set(catalog.TASKS) == {t.id for t in all_tasks()}
    assert set(catalog.FLOWS) == set(FlowType) - {FlowType.FREEFORM}
    for name, description in catalog.TASKS.values():
        assert name.strip() and description.strip()
    for tag, name, description in catalog.FLOWS.values():
        assert tag.strip() and name.strip() and description.strip()
    labels = [catalog.task_label(t.id) for t in all_tasks()]
    assert len(set(labels)) == len(labels)


def test_task_label_uses_the_flow_tag_and_keeps_retired_ids():
    assert catalog.task_label("c_variant_b") == "[Flow C] Checkout on an Alternate UI"
    assert catalog.task_label("drift_provider_vs_ui") == "[Flow Drift] Detect Data Mismatch"
    assert catalog.task_label("retired_task") == "retired_task"
    assert catalog.flow_label(FlowType.DRIFT) == "[Flow Drift] Data Source Drift"
    with pytest.raises(KeyError):
        catalog.task_info("retired_task")


def test_catalog_flows_group_tasks_under_their_own_flow():
    flows = catalog.flows()
    assert [f["tag"] for f in flows] == ["A", "B", "C", "D", "E", "F", "G", "H", "Drift"]
    by_flow = {f["flow_type"]: f for f in flows}
    assert by_flow["g"]["tasks"] == []  # fixture fact: no G task yet; an empty flow is still listed
    assert [t["id"] for t in by_flow["c"]["tasks"]] == ["c_variant_b"]
    for flow, f in by_flow.items():
        assert all(get(t["id"]).flow_type.value == flow for t in f["tasks"])
        assert [t["label"] for t in f["tasks"]] == sorted(t["label"] for t in f["tasks"])
    assert sum(len(f["tasks"]) for f in by_flow.values()) == len(all_tasks())
