import pytest

from harness.device.inspect import SMS_SENT
from harness.seed.generator import apply, generate_plan
from harness.tasks.registry import all_tasks, get
from harness.verify.runner import run_verifier
from harness.verify.selftest import CASES, confirm_step, format_result, run_case, run_selftest
from tests.fakes import FakeInspector

# Business tasks have their own cases and fake (tests/test_biz_tasks.py).
DEVICE_TASKS = [t for t in all_tasks() if t.id in CASES]


@pytest.mark.parametrize("task", DEVICE_TASKS, ids=lambda t: t.id)
@pytest.mark.parametrize("seed", [1, 2, 3, 4242, 2**31 + 7])
def test_every_selftest_case_behaves(task, seed):
    results = run_selftest(task, FakeInspector(), seed)
    assert [r.case for r in results] == [c.name for c in CASES[task.id]]
    bad = [format_result(r) for r in results if not r.ok]
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("task", DEVICE_TASKS, ids=lambda t: t.id)
def test_each_task_has_untouched_gold_and_a_decoy(task):
    names = [c.name for c in CASES[task.id]]
    assert names[:2] == ["untouched", "gold"]
    assert any(n.startswith("decoy_") for n in names)
    assert [c.expect_pass for c in CASES[task.id]].count(True) == 1


@pytest.mark.parametrize("offset_ms", [-90_000, 0, 90_000])
def test_f_selftest_holds_under_device_clock_skew(offset_ms):
    results = run_selftest(get("f_send_sms"), FakeInspector(clock_offset_ms=offset_ms), 5)
    assert all(r.ok for r in results), [format_result(r) for r in results if not r.ok]


def test_selftest_without_calendar_provider_still_behaves():
    for task in DEVICE_TASKS:
        assert all(r.ok for r in run_selftest(task, FakeInspector(calendar=False), 9))


def test_gold_reports_agreeing_self_report_and_decoys_disagree():
    for r in run_selftest(get("f_send_sms"), FakeInspector(), 3):
        assert r.result.self_report_agrees is (r.case == "gold")


def test_case_cleanup_restores_device_to_baseline():
    fake = FakeInspector()
    fake.push_file(f"{fake.markor_dir}/Existing.md", b"keep me")
    fake.insert_contact("Pre Existing", "(206) 555-0199", None)
    before = fake.snapshot_state()
    task = get("a_markor_note")
    run_case(task, next(c for c in CASES[task.id] if c.name == "decoy_edited_seed_note"), fake, 8)
    for t in DEVICE_TASKS:
        run_selftest(t, fake, 8)
    after = fake.snapshot_state()
    assert after.files == before.files and after.contacts == before.contacts and after.sms == before.sms


def test_f_confirmation_with_wrong_recipient_in_summary_fails():
    task = get("f_send_sms")
    fake = FakeInspector()
    plan = generate_plan(21, task)
    apply(plan, fake)
    pre = fake.snapshot_state()
    other = next(c for c in plan.contacts if c.name != plan.expected["name"])
    summary = {"recipient": other.name, "number": other.phone, "message": plan.expected["message"]}
    step = confirm_step(1, fake.device_time_ms() - fake.clock_offset_ms, summary)
    fake.insert_sms(plan.expected["phone"], plan.expected["message"], SMS_SENT, fake.device_time_ms() + 3000)
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [step])
    assert all(r.passed for r in res.end_state)
    assert [r.name for r in res.process if not r.passed] == ["sent_matches_approved_summary"]
    assert not res.passed


def test_f_reject_and_nothing_sent_fails_end_state_but_not_ordering():
    task = get("f_send_sms")
    fake = FakeInspector()
    plan = generate_plan(22, task)
    apply(plan, fake)
    pre = fake.snapshot_state()
    step = confirm_step(1, fake.device_time_ms(), {"to": plan.expected["name"]}, "reject")
    res = run_verifier(task, plan, pre, fake.snapshot_state(), None, [step])
    process = {r.name: r.passed for r in res.process}
    assert process == {"approved_confirmation_requested": False, "no_send_before_approval": True,
                       "sent_matches_approved_summary": False}
    assert not any(r.passed for r in res.end_state)
