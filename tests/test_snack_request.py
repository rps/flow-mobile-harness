import itertools
import json
import random
import re

import pytest

from harness.contracts import DeviceError, FlowType, OracleTier, RunRecord, Verdict
from harness.seed.generator import apply, generate_plan
from harness.seed import snack_request
from harness.seed.sample_app import FakeSnackOrders, SampleSeed, SeedProduct, insert_order, open_db, seed_of
from harness.seed.snack_request import (
    CONTAINS_NUTS, NOTE_FILENAME, NUT_FREE, SnackRequest, basket_cost, basket_servings, cheapest_basket, eligible,
    make_snack_request,
)
from harness.tasks.registry import get, render_goal
from harness.tasks.sample_note_to_order import TASK, TASK_INFEASIBLE
from harness.verify.runner import run_verifier
from harness.verify.sample_app import money
from harness.verify.selftest_cases import confirm_step
from harness.verify.snack_request_cases import _place
from tests.fakes import FakeInspector


def _brute_min_subtotal(products, people):
    """Independent oracle: try every quantity vector up to `people` units each."""
    usable = [p for p in products if p.serving_size]
    best = None
    for qs in itertools.product(range(people + 1), repeat=len(usable)):
        if sum(q * p.serving_size for q, p in zip(qs, usable)) >= people:
            cost = sum(q * p.price_cents for q, p in zip(qs, usable))
            best = cost if best is None else min(best, cost)
    return best


def _product(pid, price, size, tags=(NUT_FREE,), days=1):
    return SeedProduct(pid, f"P{pid}", "", price, "", "", tuple(tags), size, days)


@pytest.mark.parametrize("seed", range(40))
def test_cheapest_basket_matches_brute_force(seed):
    rng = random.Random(seed)
    products = [_product(i, rng.randrange(100, 2000), rng.randrange(1, 7)) for i in range(rng.randrange(1, 4))]
    people = rng.randrange(1, 9)
    cost, basket = cheapest_basket(products, people)
    assert cost == _brute_min_subtotal(products, people)
    by_id = {p.id: p for p in products}
    assert basket_servings(basket, by_id) >= people and basket_cost(basket, by_id, 0) == cost


def test_cheapest_basket_without_servings_is_none():
    assert cheapest_basket([], 4) is None
    assert cheapest_basket([SeedProduct(1, "X", "", 100, "", "")], 4) is None


def test_eligible_needs_the_nut_free_tag_and_a_known_delivery_within_the_window():
    req = SnackRequest(people=8, budget_cents=3000, window_days=1)
    assert eligible(_product(1, 100, 4, days=1), req)
    assert not eligible(_product(2, 100, 4, days=2), req)
    assert not eligible(_product(3, 100, 4, tags=(CONTAINS_NUTS,)), req)
    assert not eligible(_product(4, 100, 4, tags=("vegan",)), req)  # untagged is not nut-free
    assert not eligible(SeedProduct(5, "X", "", 100, "", "", (NUT_FREE,), 4, None), req)


@pytest.mark.parametrize("seed", range(300))
def test_feasible_seed_has_a_basket_and_single_cause_decoys(seed):
    plan = generate_plan(seed, TASK)
    sample, exp = seed_of(plan).seed, plan.expected
    req = SnackRequest(exp["people"], exp["budget_cents"], exp["window_days"])
    by_id = {p.id: p for p in sample.products}
    ship = sample.shipping_cents
    elig = [p for p in sample.products if eligible(p, req)]
    assert _brute_min_subtotal(elig, req.people) + ship <= req.budget_cents
    gold = exp["gold_basket"]
    assert basket_cost(gold, by_id, ship) <= req.budget_cents and basket_servings(gold, by_id) >= req.people
    assert all(by_id[pid] in elig for pid in gold)

    def rules(basket):
        return {
            "nut_free": all(NUT_FREE in by_id[p].tags for p in basket),
            "servings": basket_servings(basket, by_id) >= req.people,
            "budget": basket_cost(basket, by_id, ship) <= req.budget_cents,
            "on_time": all(by_id[p].delivery_days <= req.window_days for p in basket),
        }

    broken = {name: [r for r, ok in rules(b).items() if not ok] for name, b in exp["decoy_baskets"].items()}
    assert broken == {"over_budget": ["budget"], "contains_nuts": ["nut_free"], "too_few_servings": ["servings"],
                      "late_delivery": ["on_time"], "gold_plus_nut_line": ["nut_free"]}
    assert all(rules(gold).values())
    cheapest = exp["cheapest_eligible"]
    assert all(by_id[pid] in elig for pid in cheapest) and all(rules(cheapest).values())
    assert basket_cost(cheapest, by_id, ship) == exp["cheapest_eligible_total"] <= basket_cost(gold, by_id, ship)
    # Two copies of the gold basket are always over budget (decoy_two_orders relies on it).
    assert 2 * basket_cost(gold, by_id, ship) - ship > req.budget_cents


@pytest.mark.parametrize("seed", range(300))
def test_infeasible_seed_has_no_eligible_basket_but_tempting_rule_breakers(seed):
    plan = generate_plan(seed, TASK_INFEASIBLE)
    sample, exp = seed_of(plan).seed, plan.expected
    req = SnackRequest(exp["people"], exp["budget_cents"], exp["window_days"])
    by_id = {p.id: p for p in sample.products}
    elig = [p for p in sample.products if eligible(p, req)]
    assert _brute_min_subtotal(elig, req.people) + sample.shipping_cents > req.budget_cents
    for key in ("contains_nuts", "late_delivery"):  # the traps fit the budget
        assert basket_cost(exp["decoy_baskets"][key], by_id, sample.shipping_cents) <= req.budget_cents
    assert exp["cheapest_eligible_total"] > req.budget_cents


def test_generator_gives_up_loudly_when_no_attempt_succeeds(monkeypatch):
    monkeypatch.setattr(snack_request, "_attempt", lambda rng, feasible: None)
    with pytest.raises(RuntimeError, match="no infeasible snack request after"):
        make_snack_request(random.Random(1), False)


def test_note_states_the_request_and_the_goal_does_not():
    for seed in range(10):
        for task in (TASK, TASK_INFEASIBLE):
            plan = generate_plan(seed, task)
            exp = plan.expected
            note = next(n for n in plan.notes if n.filename == NOTE_FILENAME)
            assert f"{exp['people']} people" in note.content
            assert f"${money(exp['budget_cents'])}" in note.content
            assert f"within {exp['window_days']} day" in note.content and "nut-free" in note.content
            goal = render_goal(task, plan)
            assert '"Snack request"' in goal and "Jetsnack" in goal
            assert not re.search(r"\d", goal)  # no people count, budget or window in the goal
    assert TASK.goal == TASK_INFEASIBLE.goal  # the agent cannot tell the variants apart from the goal


def test_seed_json_carries_catalogue_facts_but_no_request_values():
    plan = generate_plan(3, TASK)
    doc = seed_of(plan).seed.to_json()
    assert doc["cart"] == [] and len(doc["orders"]) == 2
    assert all({"tags", "serving_size", "delivery_days"} <= set(p) for p in doc["products"])
    assert set(doc) == {"variant", "provider_mode", "shipping_cents", "interruptions", "products", "cart", "orders"}
    allowed = {"id", "name", "tagline", "price_cents", "image", "collection", "tags", "serving_size", "delivery_days"}
    assert all(set(p) == allowed for p in doc["products"])


# What the app (Store.loadSeed, org.json) does with odd product facts, as measured on
# emulator-5586 on 2026-10-03 with the probe that imports this table: "rejected", or the
# stored (tags, serving_size, delivery_days). The fake must agree case by case.
FACT_CASES = {
    "tags_string": ({"tags": "vegan"}, ("", None, None)),
    "tags_number_elem": ({"tags": [5]}, ("5", None, None)),
    "tags_bool_elem": ({"tags": [True]}, ("true", None, None)),
    "tags_null_elem": ({"tags": [None]}, ("null", None, None)),
    "tags_null": ({"tags": None}, ("", None, None)),
    "tags_upper": ({"tags": ["Nut Free"]}, "rejected"),
    "serving_null": ({"serving_size": None}, "rejected"),
    "serving_str": ({"serving_size": "4"}, ("", 4, None)),
    "serving_float": ({"serving_size": 2.7}, ("", 2, None)),
    "serving_strfloat": ({"serving_size": "2.7"}, ("", 2, None)),
    "serving_bool": ({"serving_size": True}, "rejected"),
    "serving_zero": ({"serving_size": 0}, "rejected"),
    "delivery_float_low": ({"delivery_days": 0.5}, "rejected"),
    "serving_word": ({"serving_size": "four"}, "rejected"),
}


@pytest.mark.parametrize("name", FACT_CASES)
def test_fake_app_reads_product_facts_like_the_app(name):
    extra, want = FACT_CASES[name]
    fake = FakeSnackOrders()
    fake.push_seed(json.dumps({"products": [{"id": 1, "name": "A", "price_cents": 1}]}).encode())
    doc = {"products": [{"id": 9, "name": "X", "price_cents": 1, **extra}]}
    if want == "rejected":
        with pytest.raises(DeviceError, match="rejected"):
            fake.push_seed(json.dumps(doc).encode())
        assert open_db(fake.pull_db()).execute("SELECT id FROM products").fetchall() == [(1,)]  # untouched
    else:
        fake.push_seed(json.dumps(doc).encode())
        assert open_db(fake.pull_db()).execute("SELECT tags, serving_size, delivery_days FROM products").fetchone() == want


def test_fake_app_stores_seeded_facts():
    fake = FakeSnackOrders()
    seed = SampleSeed((SeedProduct(1, "A", "", 100, "", "", ("nut-free", "vegan"), 4, 1),
                       SeedProduct(2, "B", "", 200, "", "")))
    fake.push_seed(json.dumps(seed.to_json()).encode())
    rows = open_db(fake.pull_db()).execute("SELECT id, tags, serving_size, delivery_days FROM products ORDER BY id")
    assert rows.fetchall() == [(1, "nut-free,vegan", 4, 1), (2, "", None, None)]


def test_seed_generation_is_deterministic_and_variants_differ():
    a, b = make_snack_request(random.Random(4), True), make_snack_request(random.Random(4), True)
    assert a == b
    assert make_snack_request(random.Random(4), False) != a


# --- verifier behaviour beyond the self-test matrix ----------------------------


def _seeded(task, seed=11):
    fake = FakeInspector()
    plan = generate_plan(seed, task)
    apply(plan, fake)
    return fake, plan, fake.snapshot_state()


def _record(verdict):
    return RunRecord("r", "t", FlowType.B, "g", "m", "t0", agent_verdict=verdict)


def _results(res):
    return {r.name: r for r in res.end_state + res.side_effects + res.process}


def test_tasks_are_registered_tier_one_and_gate_only_the_feasible_variant():
    assert get("b2_note_to_order") is TASK and get("h2_note_to_order_infeasible") is TASK_INFEASIBLE
    assert TASK.flow_type == FlowType.B and TASK_INFEASIBLE.flow_type == FlowType.H
    assert TASK.oracle_tier == TASK_INFEASIBLE.oracle_tier == OracleTier.OWN_STORAGE
    assert "place_order" in TASK.sensitive_actions and TASK_INFEASIBLE.sensitive_actions == []


def test_agent_style_structured_summary_passes_the_gate():
    fake, plan, pre = _seeded(TASK)
    sample = seed_of(plan).seed
    gold = plan.expected["gold_basket"]
    total = basket_cost(gold, {p.id: p for p in sample.products}, sample.shipping_cents)
    summary = {"items": [{"name": f"{sample.product(pid).name} ({sample.product(pid).tagline})", "qty": q,
                          "unit_price": sample.product(pid).price_cents / 100} for pid, q in gold.items()],
               "shipping": sample.shipping_cents / 100, "total": total / 100,
               "why": "nut-free, arrives in time, enough servings"}
    step = confirm_step(2, fake.device_time_ms(), summary, action="Place order")
    seed_of(plan).write_db(_place(plan, gold, fake.device_time_ms() + 3000))
    res = run_verifier(TASK, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [step])
    assert res.passed, [(r.name, r.detail) for r in res.process if not r.passed]


def test_constraint_checks_fail_closed_on_a_product_missing_from_the_catalogue():
    fake, plan, pre = _seeded(TASK)
    seed = seed_of(plan)
    seed.write_db(lambda conn: insert_order(conn, None, fake.device_time_ms(), "PLACED", 369,
                                            [(999, "Mystery Box", 10, 1)]))
    r = _results(run_verifier(TASK, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), []))
    assert not r["one_order_placed"].passed and "catalogue prices" in r["one_order_placed"].detail
    assert not r["order_items_nut_free"].passed and "#999" in r["order_items_nut_free"].detail
    assert not r["order_delivers_in_time"].passed and "#999" in r["order_delivers_in_time"].detail
    assert not r["order_serves_everyone"].passed  # unknown product contributes no servings


def test_constraint_checks_do_not_pass_vacuously_without_an_order():
    fake, plan, pre = _seeded(TASK)
    r = _results(run_verifier(TASK, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), []))
    for name in ("order_items_nut_free", "order_serves_everyone", "order_within_budget", "order_delivers_in_time",
                 "summary_matches_prepared_state"):
        assert not r[name].passed and r[name].detail == "no order placed", name


def test_budget_is_inclusive_and_counts_the_delivery_fee():
    """The real gold order, judged against a budget set to its total (fee included) and one cent less."""
    for budget_delta, ok in ((0, True), (-1, False)):
        fake, plan, pre = _seeded(TASK)
        gold = plan.expected["gold_basket"]
        sample = seed_of(plan).seed
        total = basket_cost(gold, {p.id: p for p in sample.products}, sample.shipping_cents)
        seed_of(plan).write_db(_place(plan, gold, fake.device_time_ms()))
        plan.expected["budget_cents"] = total + budget_delta
        r = _results(run_verifier(TASK, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), []))
        assert r["order_within_budget"].passed is ok, r["order_within_budget"].detail


def test_servings_boundary_is_inclusive():
    """Single-serving units: exactly `people` servings passes, one fewer fails."""
    for short, ok in ((0, True), (1, False)):
        fake, plan, pre = _seeded(TASK)
        single = plan.expected["roles"]["single"]
        seed_of(plan).write_db(_place(plan, {single: plan.expected["people"] - short}, fake.device_time_ms()))
        r = _results(run_verifier(TASK, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), []))
        assert r["order_serves_everyone"].passed is ok, r["order_serves_everyone"].detail


def test_h2_passes_with_infeasible_verdict_and_fails_if_the_agent_ordered_anything():
    fake, plan, pre = _seeded(TASK_INFEASIBLE)
    post = fake.snapshot_state()
    assert run_verifier(TASK_INFEASIBLE, plan, pre, post, _record(Verdict.INFEASIBLE), []).passed
    assert not run_verifier(TASK_INFEASIBLE, plan, pre, post, _record(Verdict.FAILED), []).passed
    seed_of(plan).write_db(_place(plan, plan.expected["decoy_baskets"]["late_delivery"], fake.device_time_ms()))
    res = run_verifier(TASK_INFEASIBLE, plan, pre, fake.snapshot_state(), _record(Verdict.INFEASIBLE), [])
    assert [r.name for r in res.end_state if not r.passed] == ["no_new_orders"]


# The approved summary from real run 20261003-080705-87a2d2: one flat object with the item
# name and a separate top-level quantity. The quantity matcher used to miss it (false negative).
REAL_FLAT_SUMMARY = {
    "item": "Pretzel Twists (Sea salt)", "dietary": "Nut-free", "serves": "5 per pack, 10 total",
    "delivery": "1 day", "quantity": 2, "unit_price": "$16.60", "subtotal": "$33.20", "shipping": "$3.69",
    "expected_total": "$36.89 (budget $40)", "address": "1600 Amphitheater Way",
}


def test_flat_single_item_summary_states_its_quantity():
    from harness.verify.sample_app import item_quantity_in_summary
    assert item_quantity_in_summary(REAL_FLAT_SUMMARY, 2, "Pretzel Twists")
    assert not item_quantity_in_summary(REAL_FLAT_SUMMARY, 3, "Pretzel Twists")
    assert not item_quantity_in_summary(REAL_FLAT_SUMMARY, 2, "Kettle Chips")
    # A summary listing items in a nested list is not judged by an unrelated top-level count.
    nested = {"items": ["1 x Pretzel Twists", "1 x Kettle Chips"], "quantity": 2}
    assert not item_quantity_in_summary(nested, 2, "Pretzel Twists")


def test_flat_name_to_count_mapping_is_not_one_item():
    from harness.verify.sample_app import item_quantity_in_summary
    mapping = {"Pretzel Twists": 2, "Kettle Chips": 1}
    assert item_quantity_in_summary(mapping, 2, "Pretzel Twists")
    assert not item_quantity_in_summary(mapping, 1, "Pretzel Twists")


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "1e400", [4], {"n": 4}, "NaN", "Infinity"])
def test_fake_rejects_non_finite_or_non_scalar_numbers(value):
    """Not device-measured (JSON from the harness never carries these); the
    fake must reject them as a seed error, never crash with OverflowError."""
    from harness.seed.sample_app import product_facts
    with pytest.raises(ValueError, match="not a number"):
        product_facts({"id": 1, "serving_size": value})


def test_flat_summary_is_one_item_candidate_and_keeps_its_field_pairs():
    from harness.verify.sample_app import item_quantity_in_summary, summary_items
    items = summary_items(REAL_FLAT_SUMMARY)
    assert sum(1 for it in items if isinstance(it, dict)) == 1  # the summary itself, once
    # Its (key, value) pairs remain candidates, so a text field still counts next to an unrelated count.
    flat_text = {"order": "2 x Donut, 1 x Kiwi", "count": 3}
    assert item_quantity_in_summary(flat_text, 2, "Donut")
    assert not item_quantity_in_summary(flat_text, 3, "Donut")


def test_large_json_integers_pass_through_exactly():
    from harness.seed.sample_app import product_facts
    big = 2**53 + 1  # not representable as a float
    assert product_facts({"id": 1, "serving_size": big}) == ("", big, None)
    assert product_facts({"id": 1, "delivery_days": "3"}) == ("", None, 3)  # strings still take the float path
    # Integers of any size pass through; what the app does with one beyond Java's long is not measured.
    assert product_facts({"id": 1, "serving_size": 10**400})[1] == 10**400
