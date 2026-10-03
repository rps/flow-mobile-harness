import re

import pytest

from harness.contracts import FlowType, OracleTier
from harness.seed.generator import generate_plan
from harness.tasks.registry import all_tasks, get, render_goal

HINT_WORDS = ["verif", "check", "provider", "database", "/sdcard", "file", "adb", "seed",
              "confirm", "approv", "test", "expected"]


def test_registry_lists_and_gets_tasks():
    ids = [t.id for t in all_tasks()]
    assert ids == ["a_markor_note", "b_contact_to_note", "f_send_sms",
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
