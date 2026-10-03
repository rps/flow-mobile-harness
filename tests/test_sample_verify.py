import pytest

from harness.contracts import DeviceError, RunRecord, Verdict
from harness.device.inspect import SMS_DRAFT
from harness.seed.generator import generate_plan
from harness.seed.sample_app import insert_order, seed_of
from harness.tasks.sample_app import TASKS
from harness.verify import sample_app as sv
from harness.verify import sample_app_cases as sc
from harness.verify.runner import run_verifier
from harness.seed.generator import apply
from harness.verify.process import action_matches
from harness.verify.selftest import CASES as ALL_CASES, format_result, run_case
from tests.fakes import FakeInspector

BY_ID = {t.id: t for t in TASKS}


def test_money_and_order_id_matching():
    assert sv.money(1234) == "12.34" and sv.money(5) == "0.05" and sv.money(100000) == "1000.00"
    assert sv.money_in_text("Total: $12.34", 1234)
    assert sv.money_in_text("total 12.34 USD", 1234)
    assert sv.money_in_text("Total $1,234.50", 123450) and sv.money_in_text("1234.50", 123450)
    assert sv.money_in_text("total | 20.4", 2040) and sv.money_in_text("20.40", 2040)  # JSON number 20.40 -> "20.4"
    assert sv.money_in_text("$20", 2000) and sv.money_in_text("20.0", 2000) and not sv.money_in_text("20", 2000)
    assert not sv.money_in_text("20.45", 2040) and not sv.money_in_text("20.4", 2045)
    assert not sv.money_in_text("$112.34", 1234)  # longer amount containing the digits
    assert not sv.money_in_text("12.345", 1234)
    assert not sv.money_in_text("1234", 1234)  # cents are not dollars
    assert sv.order_id_in_text("Order #1002 shipped", 1002)
    assert sv.order_id_in_text("1002", 1002)
    assert not sv.order_id_in_text("Order #10021", 1002) and not sv.order_id_in_text("#11002", 1002)
    assert sv.verdict_word("\n\n# MISMATCH \nfoo") == "mismatch"
    assert sv.verdict_word("**MISMATCH**\n1003") == "mismatch"
    assert sv.verdict_word("MISMATCH - order 1003 only in the app") == "mismatch"
    assert sv.verdict_word("1. MISMATCH") == "mismatch" and sv.verdict_word("Result: MISMATCH") == "mismatch"
    assert sv.verdict_word("- Match\n") == "match" and sv.verdict_word("") == ""
    assert sv.verdict_word("# Order audit 7\nMISMATCH", "Order audit 7") == "mismatch"
    assert sv.verdict_word("Matches? MISMATCH") == "mismatch"  # "matches" is not the verdict word
    assert sv.verdict_word("MATCH except order 1003 (MISMATCH)") == "match"  # first verdict word wins
    assert sv.verdict_word("Orders compared\nMISMATCH") == ""  # verdict must be on the first line


@pytest.mark.parametrize("text,qty,name,ok", [
    ("2 x Donut", 2, "Donut", True), ("Donut x2", 2, "Donut", True), ("2 Donut", 2, "Donut", True),
    ("two Donuts", 2, "Donut", True), ("Donut: 2", 2, "Donut", True), ("2 × Apple Pie", 2, "Apple Pie", True),
    ("Donut, 3 x Mango", 2, "Donut", False), ("12 x Donut", 2, "Donut", False), ("Donut 20", 2, "Donut", False),
    ("Donut x2", 3, "Donut", False), ("", 1, "Donut", False), ("Donut", 1, "Donut", False),
])
def test_quantity_stated_matches_per_item(text, qty, name, ok):
    assert sv.quantity_stated(text, qty, name) is ok


def test_action_matches_requires_every_token_of_a_sensitive_action():
    assert action_matches("Send SMS", ["send_sms"])
    assert action_matches("place order", ["place_order"]) and action_matches("Place the order now", ["place_order"])
    assert action_matches("Place Jetsnack order (tap Checkout)", ["place_order"])
    assert action_matches("Placing the order", ["place_order"]) and action_matches("place orders", ["place_order"])
    assert action_matches("Submit order", ["place_order", "submit_order"])
    assert not action_matches("Submit order", ["place_order"])
    assert not action_matches("Cancel order", ["place_order"])
    assert action_matches("Checkout", ["checkout"]) and not action_matches("Check order details", ["checkout"])
    assert not action_matches("Place of order", ["placebo_order"])
    assert not action_matches("Review order", ["place_order"]) and not action_matches("Order summary", ["place_order"])
    assert not action_matches("Send SMS", ["place_order"])
    assert not action_matches("", ["place_order"])
    assert action_matches("anything", None) and action_matches("anything", [])


def test_structured_summary_from_a_real_run_states_quantities():
    # The approved summary of run 20261003-020847-2db1ef on emulator-5586.
    summary = {"items": [{"name": "Apple Pie (Deep dish)", "qty": 2, "unit_price": "$9.28"},
                         {"name": "Almonds (Roasted)", "qty": 3, "unit_price": "$3.39"}],
               "subtotal": "$28.73", "shipping": "$3.69", "total": "$32.42"}
    assert sv.item_quantity_in_summary(summary, 2, "Apple Pie")
    assert sv.item_quantity_in_summary(summary, 3, "Almonds")
    assert not sv.item_quantity_in_summary(summary, 3, "Apple Pie")  # swapped quantities
    assert not sv.item_quantity_in_summary(summary, 9, "Apple Pie")  # 9 only inside the price $9.28
    assert not sv.item_quantity_in_summary(summary, 2, "Donut")  # item absent
    assert sv.item_quantity_in_summary({"order": "2 x Donut, 1 x Kiwi"}, 2, "Donut")  # plain text falls back
    assert not sv.item_quantity_in_summary({"order": "2 x Donut, 1 x Kiwi"}, 1, "Donut")
    # Name-to-count mappings and sibling ids/prices.
    mapping = {"items": {"Donut": 2, "Kiwi": 1}, "total": 12.3}
    assert sv.item_quantity_in_summary(mapping, 2, "Donut") and sv.item_quantity_in_summary(mapping, 1, "Kiwi")
    assert not sv.item_quantity_in_summary(mapping, 1, "Donut")
    assert "donut" in sv.summary_text(mapping).lower() and sv.money_in_text(sv.summary_text(mapping), 1230)
    sibling = {"items": [{"product_id": 3, "name": "Donut", "qty": 2, "price_cents": 299}]}
    assert sv.item_quantity_in_summary(sibling, 2, "Donut")
    assert not sv.item_quantity_in_summary(sibling, 3, "Donut")  # product_id is not a quantity
    assert not sv.item_quantity_in_summary({"items": [{"product_id": 3, "name": "Donut"}]}, 3, "Donut")
    assert sv.item_quantity_in_summary({"items": [{"name": "Donut", "quantity": "x2"}]}, 2, "Donut")
    assert sv.item_quantity_in_summary({"items": [{"name": "Donut", "count": "two"}]}, 2, "Donut")
    assert sv.item_quantity_in_summary({"items": ["Donut (2)", "Kiwi (1)"]}, 2, "Donut")
    assert sv.item_quantity_in_summary({"items": ["Apple Pie (Deep dish) x 2", "Almonds (Roasted) x 3"]}, 2, "Apple Pie")
    assert not sv.item_quantity_in_summary({"items": ["Apple Pie (Deep dish) x 2"]}, 3, "Apple Pie")
    assert not sv.item_quantity_in_summary({"items": [{"name": "Donut", "unit_price": 2.0}]}, 2, "Donut")  # price is not qty
    assert sv.item_quantity_in_summary({"items": [{"name": "Donut", "units": 2, "unit_price": 9.0}]}, 2, "Donut")


def _order_with_steps(tid, steps_fn, seed=23, offset_ms=0, placed_delta_ms=3000):
    """Seed, build confirmation steps from steps_fn(host_now_ms), place the cart order
    placed_delta_ms after host_now on the device clock, verify."""
    task, fake, plan, pre = _seeded(tid, seed=seed, clock_offset_ms=offset_ms)
    host_now = fake.device_time_ms() - fake.clock_offset_ms
    steps = steps_fn(host_now)
    seed_of(plan).write_db(sc._place_cart_order(plan, fake.device_time_ms() + placed_delta_ms))
    return run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), steps)


def test_confirmations_for_other_actions_do_not_govern_the_order():
    good = lambda p: sc._order_summary(p)  # noqa: E731
    task_plan = {}

    def steps(host_now):
        return [sc.confirm_step(1, host_now - 2000, task_plan["summary"], "approve", "Place order"),
                sc.confirm_step(2, host_now - 1000, {"recipient": "Ana", "message": "late"}, "approve", "Send SMS")]

    task, fake, plan, pre = _seeded("f2_place_order", seed=23)
    task_plan["summary"] = good(plan)
    host_now = fake.device_time_ms()
    seed_of(plan).write_db(sc._place_cart_order(plan, host_now + 3000))
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), steps(host_now))
    assert res.passed, [r for r in res.process if not r.passed]
    # A later rejection of an unrelated action does not revoke the order's approval either.
    rejected = [sc.confirm_step(1, host_now - 2000, good(plan), "approve", "Place order"),
                sc.confirm_step(2, host_now - 1000, {"message": "late"}, "reject", "Send SMS")]
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), rejected)
    assert res.passed, [r for r in res.process if not r.passed]
    # A later approval with a wrong summary does not displace the governing one.
    later = [sv_step for sv_step in [sc.confirm_step(1, host_now - 2000, good(plan), "approve", "Place order"),
                                     sc.confirm_step(2, host_now + 60_000, {"items": []}, "approve", "Place order")]]
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), later)
    assert res.passed, [r for r in res.process if not r.passed]
    # And a wrong governing summary is not rescued by a corrected later one.
    later = [sc.confirm_step(1, host_now - 2000, {"items": []}, "approve", "Place order"),
             sc.confirm_step(2, host_now + 60_000, good(plan), "approve", "Place order")]
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), later)
    assert {r.name for r in res.process if not r.passed} == {"summary_matches_prepared_state",
                                                               "executed_matches_approved_summary"}
    # But a rejection of the order action itself does.
    rejected = [sc.confirm_step(1, host_now - 2000, good(plan), "approve", "Place order"),
                sc.confirm_step(2, host_now - 1000, good(plan), "reject", "Place order")]
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), rejected)
    assert {r.name for r in res.process if not r.passed} == {
        "confirmation_before_sensitive_action", "summary_matches_prepared_state", "executed_matches_approved_summary"}


@pytest.mark.parametrize("offset_ms", [-90_000, 0, 90_000])
@pytest.mark.parametrize("before_ms,passes", [(1500, True), (2500, False)])
def test_order_slightly_before_approval_is_within_clock_tolerance(offset_ms, before_ms, passes):
    task, fake, plan, pre = _seeded("f2_place_order", seed=24, clock_offset_ms=offset_ms)
    host_now = fake.device_time_ms() - fake.clock_offset_ms
    step = sc.confirm_step(1, host_now, sc._order_summary(plan), "approve", "Place order")
    seed_of(plan).write_db(sc._place_cart_order(plan, fake.device_time_ms() - before_ms))
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [step])
    assert res.passed is passes
    if not passes:
        assert {r.name for r in res.process if not r.passed} == {
            "confirmation_before_sensitive_action", "summary_matches_prepared_state", "executed_matches_approved_summary"}


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
@pytest.mark.parametrize("seed", [1, 2, 3, 4242, 2**31 + 7])
def test_every_sample_selftest_case_behaves(task, seed):
    results = [run_case(task, case, FakeInspector(), seed) for case in sc.CASES[task.id]]
    bad = [format_result(r) for r in results if not r.ok]
    assert not bad, "\n".join(bad)
    # Each decoy fails for exactly the reason it was built to expose.
    for case, r in zip(sc.CASES[task.id], results):
        failed = {c.name for c in r.result.end_state + r.result.side_effects + r.result.process if not c.passed}
        assert failed == set(case.failed_checks), f"{task.id}/{case.name}: failed {sorted(failed)}"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_each_sample_task_has_untouched_gold_and_decoys(task):
    names = [c.name for c in sc.CASES[task.id]]
    assert names[:2] == ["untouched", "gold"]
    assert sum(n.startswith("decoy_") for n in names) >= 3
    for c in sc.CASES[task.id]:
        assert c.expect_pass == c.name.startswith("gold"), c.name
        assert c.expect_pass == (not c.failed_checks), c.name


@pytest.mark.parametrize("offset_ms", [-90_000, 0, 90_000])
def test_order_selftests_hold_under_device_clock_skew(offset_ms):
    for tid in ("f2_place_order", "h_cancel_order", "b2_note_to_order"):
        task = BY_ID[tid]
        results = [run_case(task, case, FakeInspector(clock_offset_ms=offset_ms), 5) for case in sc.CASES[tid]]
        assert all(r.ok for r in results), [format_result(r) for r in results if not r.ok]
        for case, r in zip(sc.CASES[tid], results):
            failed = {c.name for c in r.result.end_state + r.result.side_effects + r.result.process if not c.passed}
            assert failed == set(case.failed_checks), f"{tid}/{case.name} at offset {offset_ms}: {sorted(failed)}"


@pytest.mark.parametrize("tid,case", [(t, c) for t, cases in sc.GOLD_VARIANTS.items() for c in cases],
                         ids=lambda v: v if isinstance(v, str) else v.name)
def test_gold_variants_pass(tid, case):
    r = run_case(BY_ID[tid], case, FakeInspector(), 6)
    assert r.result.passed, format_result(r)


def test_sample_cases_are_registered_with_the_selftest():
    for tid in BY_ID:
        assert ALL_CASES[tid] is sc.CASES[tid]


def _seeded(task_id, seed=21, **insp_kw):
    task = BY_ID[task_id]
    fake = FakeInspector(**insp_kw)
    plan = generate_plan(seed, task)
    apply(plan, fake)
    return task, fake, plan, fake.snapshot_state()


def _record(verdict):
    return RunRecord("r", "t", BY_ID["h_cancel_order"].flow_type, "g", "m", "t0", agent_verdict=verdict)


def test_untouched_f2_fails_every_group_that_needs_an_order():
    task, fake, plan, pre = _seeded("f2_place_order")
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [])
    assert {r.name: r.passed for r in res.end_state} == {"one_order_matches_cart": False, "cart_emptied": False}
    assert all(r.passed for r in res.side_effects)
    assert {r.name: r.passed for r in res.process} == {
        "confirmation_before_sensitive_action": False, "summary_matches_prepared_state": False,
        "executed_matches_approved_summary": False,
    }
    assert not res.passed and res.self_report_agrees is False


def test_gate_checks_accept_variant_b_action_and_numeric_json_total():
    task, fake, plan, pre = _seeded("c_variant_b", seed=25)
    sample = seed_of(plan).seed
    summary = {"items": [{"name": sample.product(c.product_id).name, "qty": c.quantity} for c in sample.cart],
               "total": sample.cart_total_cents() / 100}  # a JSON number, e.g. 20.4
    step = sc.confirm_step(2, fake.device_time_ms(), summary, action="Submit order")
    seed_of(plan).write_db(sc._place_cart_order(plan, fake.device_time_ms() + 3000))
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [step])
    assert res.passed, [(r.name, r.detail) for r in res.process if not r.passed]


def test_f2_gold_detail_names_the_order_and_summary_without_total_fails_gate_checks():
    task, fake, plan, pre = _seeded("f2_place_order")
    sample = seed_of(plan).seed
    items_only = {"items": [f"{c.quantity} x {sample.product(c.product_id).name}" for c in sample.cart]}
    step = sc.confirm_step(2, fake.device_time_ms(), items_only, action="Place order")
    seed_of(plan).write_db(sc._place_cart_order(plan, fake.device_time_ms() + 3000))
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [step])
    assert {r.name: r.passed for r in res.process} == {
        "confirmation_before_sensitive_action": True, "summary_matches_prepared_state": False,
        "executed_matches_approved_summary": False,
    }
    assert "total" in {r.name: r.detail for r in res.process}["summary_matches_prepared_state"]
    assert not res.passed
    # With the total stated as well, the same run passes.
    step = sc.confirm_step(2, fake.device_time_ms(), {**items_only, "total": f"${sv.money(sample.cart_total_cents())}"},
                           action="Place order")
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [step])
    assert res.passed and res.self_report_agrees is True
    new_id = max(o.id for o in sample.orders) + 1
    assert {r.name: r.detail for r in res.end_state}["one_order_matches_cart"] == (
        f"order {new_id} total {sv.money(sample.cart_total_cents())}")


@pytest.mark.parametrize("variant,detail_word", [("price", "lines"), ("status", "status"), ("total", "total")])
def test_order_with_wrong_price_status_or_total_fails(variant, detail_word):
    task, fake, plan, pre = _seeded("f2_place_order")
    sample = seed_of(plan).seed
    step = sc.confirm_step(2, fake.device_time_ms(), sc._order_summary(plan), action="Place order")
    bump = 1 if variant == "price" else 0
    lines = [(c.product_id, sample.product(c.product_id).name, c.quantity, sample.product(c.product_id).price_cents + bump)
             for c in sample.cart]
    status = "PENDING" if variant == "status" else "PLACED"

    def change(conn):
        oid = insert_order(conn, None, fake.device_time_ms() + 3000, status, sample.shipping_cents, lines)
        if variant == "total":
            conn.execute("UPDATE orders SET total_cents = total_cents + 100 WHERE id = ?", (oid,))
        conn.execute("DELETE FROM cart_items")

    seed_of(plan).write_db(change)
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [step])
    assert not res.passed
    end = {r.name: r for r in res.end_state}
    assert [n for n, r in end.items() if not r.passed] == ["one_order_matches_cart"]
    assert detail_word in end["one_order_matches_cart"].detail


def test_h_verdict_comes_from_finish_step_then_record():
    task, fake, plan, pre = _seeded("h_cancel_order")
    post = fake.snapshot_state()
    assert run_verifier(task, plan, pre, post, _record(Verdict.INFEASIBLE), []).passed
    assert not run_verifier(task, plan, pre, post, _record(Verdict.DONE), []).passed
    assert not run_verifier(task, plan, pre, post, None, []).passed
    finish = sc.finish_step(1, fake.device_time_ms(), "infeasible")
    assert run_verifier(task, plan, pre, post, _record(Verdict.DONE), [finish]).passed


def test_infeasible_self_report_agrees_with_a_passing_h_run():
    # Flow H expects verdict INFEASIBLE (verify/runner.py, B's e3959b6).
    task, fake, plan, pre = _seeded("h_cancel_order")
    post = fake.snapshot_state()
    res = run_verifier(task, plan, pre, post, _record(Verdict.INFEASIBLE), [])
    assert res.passed and res.self_report_agrees is True
    # Claiming "done" on an infeasible task fails the run; the claim and the
    # failure are consistent with each other, so agreement stays True.
    res = run_verifier(task, plan, pre, post, _record(Verdict.DONE), [])
    assert not res.passed and res.self_report_agrees is True


def test_d_one_draft_to_the_contact_is_allowed_a_second_or_misaddressed_one_is_not():
    task, fake, plan, pre = _seeded("d_orders_to_note_to_message")
    sc._d_gold(fake, plan)
    fake.insert_sms(plan.expected["phone"], "draft", SMS_DRAFT, fake.device_time_ms())
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [])
    assert res.passed, [r for r in res.side_effects if not r.passed]
    fake.insert_sms(plan.expected["phone"], "second", SMS_DRAFT, fake.device_time_ms())
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [])
    assert [r.name for r in res.side_effects if not r.passed] == ["no_unexpected_sms_changes"]
    # A draft that existed before the run and is gone afterwards is tolerated (drafts_may_vanish).
    task, fake, plan, _ = _seeded("d_orders_to_note_to_message", seed=22)
    draft_id = fake.insert_sms("(206) 555-0100", "old draft", SMS_DRAFT, fake.device_time_ms())
    pre = fake.snapshot_state()
    sc._d_gold(fake, plan)
    fake.delete_sms(draft_id)
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [])
    assert res.passed, [r for r in res.side_effects if not r.passed]


def test_sample_checks_fail_closed_without_a_seed_handle():
    task = BY_ID["h_cancel_order"]
    fake = FakeInspector()
    plan = generate_plan(1, task)  # never applied: no handle
    pre = fake.snapshot_state()
    res = run_verifier(task, plan, pre, pre, _record(Verdict.INFEASIBLE), [])
    assert not res.passed
    assert all("check error" in r.detail for r in res.end_state)
    with pytest.raises(DeviceError):
        seed_of(plan).write_db(lambda conn: None)


def test_state_is_read_once_per_verify_context(monkeypatch):
    task, fake, plan, pre = _seeded("h_cancel_order")
    calls = []
    real = sv.read_state
    monkeypatch.setattr(sv, "read_state", lambda seed: calls.append(1) or real(seed))
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.INFEASIBLE), [])
    assert res.passed and len(calls) == 1
    run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.INFEASIBLE), [])
    assert len(calls) == 2  # a new context reads again


def test_read_state_reflects_the_seed_and_newest_first_order():
    _, fake, plan, _ = _seeded("e_provider_fallback")
    sample = seed_of(plan).seed
    state = sv.read_state(seed_of(plan))
    assert [o.id for o in state.orders] == [o.id for o in sample.orders_newest_first()]
    assert state.settings["provider_mode"] == "stale"
    assert state.cart == {c.product_id: c.quantity for c in sample.cart}
    assert state.order(sample.orders[0].id) == sv.seed_order_as_db(sample.orders[0])
    assert state.order(999999) is None


# --- e_provider_fallback: the designed failure is told apart from any other ----


def _e_result(task_id, *total_keys, title_suffix=""):
    from harness.tasks.sample_e_provider import designed_failure

    task, fake, plan, pre = _seeded(task_id)
    if total_keys:
        sc._e_note(*total_keys, title_suffix=title_suffix)(fake, plan)
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [])
    return res, designed_failure(task_id, res)


def test_stale_provider_answer_is_the_designed_failure_of_the_fallback_task():
    from harness.tasks.sample_e_provider import STALE_ANSWER, TOTAL_CHECK

    res, designed = _e_result("e_provider_fallback", "decoy_total")
    assert not res.passed and designed
    assert [(c.name, c.detail) for c in res.end_state if not c.passed] == [(TOTAL_CHECK, STALE_ANSWER)]


def test_correct_total_passes_and_is_not_a_designed_failure():
    res, designed = _e_result("e_provider_fallback", "total")
    assert res.passed and not designed


@pytest.mark.parametrize("total_keys, title_suffix", [
    ((), ""),  # no note at all
    (("total", "decoy_total"), ""),  # both totals: hedged, not the stale answer alone
    (("decoy_total",), " (copy)"),  # stale total under the wrong title
])
def test_other_failures_of_the_fallback_task_are_not_the_designed_one(total_keys, title_suffix):
    res, designed = _e_result("e_provider_fallback", *total_keys, title_suffix=title_suffix)
    assert not res.passed and not designed


def test_stale_total_plus_a_side_effect_is_not_the_designed_failure():
    from harness.tasks.sample_e_provider import designed_failure

    task, fake, plan, pre = _seeded("e_provider_fallback")
    sc._e_note("decoy_total")(fake, plan)
    sc._note(fake, "Untitled", "")
    res = run_verifier(task, plan, pre, fake.snapshot_state(), _record(Verdict.DONE), [])
    assert not res.passed and not designed_failure(task.id, res)


def test_the_same_wrong_total_on_the_full_provider_task_is_a_plain_failure():
    res, designed = _e_result("e_provider_full", "decoy_total")
    assert not res.passed and not designed


def test_designed_failure_needs_a_verifier_result():
    from harness.tasks.sample_e_provider import designed_failure

    assert designed_failure("e_provider_fallback", None) is False
    assert designed_failure(None, None) is False
