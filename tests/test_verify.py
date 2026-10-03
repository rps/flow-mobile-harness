import pytest

from harness.contracts import FlowType, OracleTier, RunRecord, TaskSpec, Verdict
from harness.device.inspect import SMS_DRAFT, SMS_INBOX, SMS_SENT, Contact, DeviceState, Event, Sms, file_entry
from harness.verify.checks import (
    END_STATE, PROCESS, Check, norm_phone, norm_text, phones_in_text, phones_match,
)
from harness.verify.diff import AllowedChanges, diff_states, side_effect_results
from harness.verify.runner import expected_verdict, run_verifier
from harness.verify.selftest import confirm_step


def state(**kw):
    return DeviceState(**kw)


def test_normalisers():
    assert norm_text("  Hello\r\n  World\t ") == "hello world"
    assert norm_text(None) == ""
    assert norm_phone("+1 (415) 555-0123") == norm_phone("415.555.0123") == "4155550123"
    assert phones_match("14155550123", "(415) 555-0123")
    assert not phones_match("(415) 555-0124", "(415) 555-0123")
    assert not phones_match("", "")
    assert phones_in_text("call +1 415-555-0123 or (206) 555 0100; room 12") == ["4155550123", "2065550100"]
    assert phones_in_text("Phone: 415-555-0123\n12 Main St") == ["4155550123"]


def test_diff_detects_add_remove_change_in_every_category():
    pre = state(
        contacts={"1": Contact("1", "A"), "2": Contact("2", "B")},
        sms={"1": Sms("1", "1", SMS_SENT, 1, "1", "x")},
        events={"1": Event("1", "c", "E", 1, 2, None)},
        files={"/m/a.md": file_entry(b"a"), "/m/b.md": file_entry(b"b")},
    )
    post = state(
        contacts={"1": Contact("1", "A2"), "3": Contact("3", "C")},
        sms={"2": Sms("2", "1", SMS_SENT, 1, "1", "y")},
        events={"1": Event("1", "c", "E", 1, 3, None), "2": Event("2", "c", "F", 1, 2, None)},
        files={"/m/a.md": file_entry(b"a!"), "/m/c.md": file_entry(b"c")},
    )
    d = diff_states(pre, post)
    assert (d.contacts_added, d.contacts_removed, d.contacts_changed) == (["3"], ["2"], ["1"])
    assert (d.sms_added, d.sms_removed) == (["2"], ["1"])
    assert (d.events_added, d.events_changed) == (["2"], ["1"])
    assert (d.files_added, d.files_removed, d.files_modified) == (["/m/c.md"], ["/m/b.md"], ["/m/a.md"])
    assert not any(r.passed for r in side_effect_results(d, pre, post, AllowedChanges(files_added=1)))


def test_calendar_becoming_unreadable_is_a_side_effect_failure():
    d = diff_states(state(events={}), state(events=None))
    res = {r.name: r for r in side_effect_results(d, state(events={}), state(events=None), AllowedChanges())}
    assert not res["no_unexpected_calendar_changes"].passed
    d2 = diff_states(state(events=None), state(events=None))
    assert all(r.passed for r in side_effect_results(d2, state(events=None), state(events=None), AllowedChanges()))


def test_allowed_changes_bound_sms_and_drafts():
    pre = state(sms={"1": Sms("1", "1", SMS_DRAFT, 1, "5", "draft")})
    one_sent = state(sms={"2": Sms("2", "1", SMS_SENT, 2, "5", "x")})
    allowed = AllowedChanges(outgoing_sms_added=1, drafts_may_vanish=True)
    res = side_effect_results(diff_states(pre, one_sent), pre, one_sent, allowed)
    assert all(r.passed for r in res)
    # A draft vanishing is not allowed unless declared.
    res = side_effect_results(diff_states(pre, one_sent), pre, one_sent, AllowedChanges(outgoing_sms_added=1))
    assert not all(r.passed for r in res)
    # Two sent, or an incoming row appearing, exceeds the allowance.
    two = state(sms={"2": Sms("2", "1", SMS_SENT, 2, "5", "x"), "3": Sms("3", "1", SMS_SENT, 3, "5", "y")})
    assert not all(r.passed for r in side_effect_results(diff_states(state(), two), state(), two, allowed))
    inbox = state(sms={"2": Sms("2", "1", SMS_INBOX, 2, "5", "x")})
    assert not all(r.passed for r in side_effect_results(diff_states(state(), inbox), state(), inbox, allowed))


def _task(checks):
    return TaskSpec("t", FlowType.A, "goal", OracleTier.OWN_STORAGE, checks=checks)


def _record(verdict):
    return RunRecord("r", "t", FlowType.A, "goal", "m", "t0", agent_verdict=verdict)


@pytest.mark.parametrize("ok,verdict,agrees", [
    (True, Verdict.DONE, True), (True, Verdict.FAILED, False),
    (False, Verdict.DONE, False), (False, Verdict.INFEASIBLE, True), (True, None, None),
])
def test_self_report_agreement_never_changes_passed(ok, verdict, agrees):
    task = _task([Check("c", END_STATE, lambda ctx: (ok, ""))])
    res = run_verifier(task, None, state(), state(), _record(verdict), [])
    assert res.passed is ok
    assert res.self_report_agrees is agrees
    assert res.oracle_tier == OracleTier.OWN_STORAGE


@pytest.mark.parametrize("ok,verdict,agrees", [
    (True, Verdict.INFEASIBLE, True),   # correctly refused, device untouched
    (True, Verdict.DONE, False),        # claimed to do the impossible; verifier saw no change
    (False, Verdict.INFEASIBLE, False), # said infeasible but changed the device anyway
    (False, Verdict.DONE, True),        # both say the run did not end as a correct refusal
    (False, Verdict.FAILED, True), (True, None, None),
])
def test_self_report_agreement_for_infeasible_goal_tasks(ok, verdict, agrees):
    task = TaskSpec("h", FlowType.H, "do the impossible", OracleTier.OWN_STORAGE,
                    checks=[Check("c", END_STATE, lambda ctx: (ok, ""))])
    res = run_verifier(task, None, state(), state(), _record(verdict), [])
    assert res.passed is ok and res.self_report_agrees is agrees


def test_expected_verdict_by_flow():
    assert expected_verdict(_task([])) is Verdict.DONE
    assert expected_verdict(TaskSpec("h", FlowType.H, "g", OracleTier.OWN_STORAGE)) is Verdict.INFEASIBLE


def test_no_end_state_checks_never_passes():
    assert not run_verifier(_task([]), None, state(), state(), None, []).passed


def test_raising_check_fails_with_detail():
    def boom(ctx):
        raise KeyError("title")
    res = run_verifier(_task([Check("c", END_STATE, boom)]), None, state(), state(), None, [])
    assert not res.passed and "KeyError" in res.end_state[0].detail


def test_unallowed_side_effect_fails_even_if_end_state_passes():
    task = _task([Check("c", END_STATE, lambda ctx: (True, ""))])
    post = state(files={"/m/x.md": file_entry(b"x")})
    res = run_verifier(task, None, state(), post, None, [])
    assert not res.passed
    assert [r.name for r in res.side_effects if not r.passed] == ["no_unexpected_file_changes"]


def test_process_checks_are_grouped_and_count():
    task = _task([Check("c", END_STATE, lambda ctx: (True, "")), Check("p", PROCESS, lambda ctx: (False, "x"))])
    res = run_verifier(task, None, state(), state(), None, [])
    assert [r.name for r in res.process] == ["p"] and not res.passed


def test_two_allowed_changes_is_a_task_definition_error():
    with pytest.raises(ValueError):
        run_verifier(_task([AllowedChanges(), AllowedChanges()]), None, state(), state(), None, [])


def test_confirm_step_round_trips_iso_time():
    from harness.verify.process import confirmations
    s = confirm_step(2, 1_790_000_000_123, {"to": "A"}, "reject")
    (c,) = confirmations([s])
    assert c.decided_at_ms == 1_790_000_000_123 and not c.approved and c.summary == {"to": "A"}
