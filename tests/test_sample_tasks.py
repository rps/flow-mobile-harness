import dataclasses
import re

import pytest

from harness.contracts import FlowType, OracleTier
from harness.seed.generator import generate_plan
from harness.seed.sample_app import seed_of
from harness.tasks.registry import render_goal
from harness.tasks.sample_app import TASKS
from harness.tasks.sample_f2_place_order import SENSITIVE_ACTIONS
from harness.verify.sample_app import money
from harness.verify.sample_app_cases import CASES
from tests.test_tasks import HINT_WORDS

BY_ID = {t.id: t for t in TASKS}


def test_task_ids_flows_and_tiers():
    assert [t.id for t in TASKS] == [
        "d_orders_to_note_to_message", "f2_place_order", "c_variant_b", "e_provider_full",
        "e_provider_fallback", "drift_provider_vs_ui", "h_cancel_order",
    ]
    assert [t.flow_type for t in TASKS] == [
        FlowType.D, FlowType.F, FlowType.C, FlowType.E, FlowType.E, FlowType.DRIFT, FlowType.H,
    ]
    assert all(t.oracle_tier == OracleTier.OWN_STORAGE for t in TASKS)
    assert BY_ID["f2_place_order"].sensitive_actions == SENSITIVE_ACTIONS and SENSITIVE_ACTIONS[0] == "place_order"
    assert BY_ID["c_variant_b"].sensitive_actions == SENSITIVE_ACTIONS and "submit_order" in SENSITIVE_ACTIONS
    assert set(CASES) == set(BY_ID)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_goal_has_no_hints_and_renders_fully(task):
    for word in HINT_WORDS:
        assert word not in task.goal.lower(), word
    for seed in range(10):
        plan = generate_plan(seed, task)
        goal = render_goal(task, plan)
        assert not re.search(r"[{}]", goal)
        for word in HINT_WORDS:
            assert word not in goal.lower(), word
        assert "Jetsnack" in goal


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_plan_is_deterministic_and_seed_matches_expected(task):
    for seed in (0, 7, 99):
        a, b = generate_plan(seed, task), generate_plan(seed, task)
        assert a == b and seed_of(a) == seed_of(b)
        assert generate_plan(seed, task) != generate_plan(seed + 1, task)


def test_goals_do_not_reveal_totals_or_answers():
    for seed in range(10):
        for tid in ("d_orders_to_note_to_message", "e_provider_full", "e_provider_fallback"):
            plan = generate_plan(seed, BY_ID[tid])
            goal = render_goal(BY_ID[tid], plan)
            totals = [plan.expected[k] for k in plan.expected if k in ("total", "decoy_total")]
            totals += [t for _, t in plan.expected.get("orders", [])]
            for t in totals:
                assert money(t) not in goal
        plan = generate_plan(seed, BY_ID["drift_provider_vs_ui"])
        goal = render_goal(BY_ID["drift_provider_vs_ui"], plan)
        assert str(plan.expected["missing_id"]) not in goal
        assert "snackorders.orders" in goal and "MISMATCH" in goal


def test_d_targets_a_seeded_contact_and_the_two_newest_orders():
    for seed in range(10):
        plan = generate_plan(seed, BY_ID["d_orders_to_note_to_message"])
        sample = seed_of(plan).seed
        newest = sample.orders_newest_first()
        assert plan.expected["orders"] == [(o.id, o.total_cents) for o in newest[:2]]
        assert plan.expected["decoy_total"] == newest[2].total_cents
        contact = next(c for c in plan.contacts if c.name == plan.expected["name"])
        assert plan.expected["phone"] == contact.phone
        assert plan.goal_params["contact_name"] == plan.expected["name"]


@pytest.mark.parametrize("seed", [3, 17, 2**31 + 7])
def test_f2_and_c_differ_only_in_variant(seed):
    f2 = generate_plan(seed, BY_ID["f2_place_order"])
    c = generate_plan(seed, BY_ID["c_variant_b"])
    assert seed_of(f2).seed.variant == "A" and seed_of(c).seed.variant == "B"
    assert dataclasses.replace(seed_of(f2).seed, variant="B") == seed_of(c).seed
    assert {k: v for k, v in f2.expected.items() if k != "variant"} == {k: v for k, v in c.expected.items() if k != "variant"}
    assert f2.expected["total"] == seed_of(f2).seed.cart_total_cents()
    assert f2.expected["decoy_total"] == f2.expected["total"] - 369
    assert BY_ID["f2_place_order"].goal == BY_ID["c_variant_b"].goal


@pytest.mark.parametrize("seed", [3, 17, 2**31 + 7])
def test_e_tasks_differ_only_in_provider_mode(seed):
    full = generate_plan(seed, BY_ID["e_provider_full"])
    stale = generate_plan(seed, BY_ID["e_provider_fallback"])
    assert seed_of(full).seed.provider_mode == "full" and seed_of(stale).seed.provider_mode == "stale"
    assert dataclasses.replace(seed_of(full).seed, provider_mode="stale") == seed_of(stale).seed
    newest = seed_of(full).seed.orders_newest_first()
    assert full.expected["total"] == newest[0].total_cents and full.expected["decoy_total"] == newest[1].total_cents
    assert full.expected["title"] == stale.expected["title"]


@pytest.mark.parametrize("seed", [11, 12, 4242])
def test_drift_seeds_stale_provider_and_names_the_newest_order(seed):
    plan = generate_plan(seed, BY_ID["drift_provider_vs_ui"])
    newest = seed_of(plan).seed.orders_newest_first()
    assert seed_of(plan).seed.provider_mode == "stale"
    assert plan.expected["missing_id"] == newest[0].id and plan.expected["decoy_id"] == newest[1].id


@pytest.mark.parametrize("seed", [11, 12, 4242])
def test_h_names_the_newest_seeded_order(seed):
    plan = generate_plan(seed, BY_ID["h_cancel_order"])
    assert plan.expected["order_id"] == seed_of(plan).seed.orders_newest_first()[0].id
    assert f"#{plan.expected['order_id']}" in render_goal(BY_ID["h_cancel_order"], plan)
