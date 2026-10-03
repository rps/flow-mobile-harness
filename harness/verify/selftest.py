"""Verifier self-test: untouched must fail, gold must pass, decoys must fail.

Gold and decoy states are injected by script through the Inspector writers,
never through the UI. Every case seeds, injects, verifies, then deletes
whatever was added since the case began.

    python -m harness.verify.selftest --serial emulator-5554 [--task f_send_sms]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from harness.contracts import RunRecord, StepRecord, TaskSpec, Verdict, VerifierResult
from harness.device.inspect import SMS_SENT, DeviceState
from harness.seed.generator import SeedPlan, apply, generate_plan, make_seed
from harness.verify.runner import run_verifier
from harness.verify.sample_app_cases import CASES as _SAMPLE_CASES
from harness.verify.selftest_cases import Case, Inject, confirm_step, host_ms as _host_ms, iso as _iso
from harness.verify.selftest_cases import untouched as _untouched

__all__ = ["Case", "CaseResult", "CASES", "confirm_step", "run_case", "run_selftest", "cleanup", "format_result", "main"]


@dataclass
class CaseResult:
    task_id: str
    case: str
    expect_pass: bool
    result: VerifierResult

    @property
    def ok(self) -> bool:
        return self.result.passed == self.expect_pass


def _note_path(insp: Any, name: str) -> str:
    return f"{insp.markor_dir.rstrip('/')}/{name}"


# --- Task a ------------------------------------------------------------------


def _a_note(title: str, content: str) -> Inject:
    def inject(insp: Any, plan: SeedPlan) -> list[StepRecord]:
        insp.push_file(_note_path(insp, f"{title}.md"), f"# {title}\n\n{content}\n".encode())
        return []
    return inject


def _a_gold(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    e = plan.expected
    # Different spacing and case from the goal, which normalisation allows.
    return _a_note(e["title"].lower(), "  " + e["content"].replace(" ", "  ") + " ")(insp, plan)


def _a_wrong_content(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    e = plan.expected
    return _a_note(e["title"], e["content"].rsplit(" ", 1)[0] + " later")(insp, plan)


def _a_wrong_title(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    e = plan.expected
    return _a_note(e["title"] + " draft", e["content"])(insp, plan)


def _a_extra_file(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _a_gold(insp, plan)
    insp.push_file(_note_path(insp, "Untitled.md"), b"\n")
    return []


def _a_edited_seed_note(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _a_gold(insp, plan)
    n = plan.notes[0]
    insp.push_file(_note_path(insp, n.filename), (n.content + "- extra line\n").encode())
    return []


# --- Task b ------------------------------------------------------------------


def _b_note(phone: str, email: str) -> Inject:
    def inject(insp: Any, plan: SeedPlan) -> list[StepRecord]:
        e = plan.expected
        digits = "".join(ch for ch in phone if ch.isdigit())
        body = f"{e['name']}\nPhone: +1 {digits[:3]}-{digits[3:6]}-{digits[6:]}\nEmail: {email.upper()}\n"
        insp.push_file(_note_path(insp, f"{e['title']}.md"), body.encode())
        return []
    return inject


def _b_gold(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    return _b_note(plan.expected["phone"], plan.expected["email"])(insp, plan)


def _b_decoy_contact(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    return _b_note(plan.expected["decoy_phone"], plan.expected["decoy_email"])(insp, plan)


def _b_missing_email(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    return _b_note(plan.expected["phone"], "")(insp, plan)


# --- Task f ------------------------------------------------------------------


def _f_summary(plan: SeedPlan, message: str | None = None) -> dict:
    e = plan.expected
    return {"recipient": e["name"], "number": e["phone"], "message": message or e["message"]}


def _send(insp: Any, address: str, body: str, delay_ms: int = 500) -> None:
    insp.insert_sms(address, body, SMS_SENT, insp.device_time_ms() + delay_ms)


def _other_phone(plan: SeedPlan) -> str:
    return next(c.phone for c in plan.contacts if c.phone != plan.expected["phone"])


def _f_case(*, decision: str | None = "approve", approve_after_send: bool = False,
            summary_message: str | None = None, address: Callable[[SeedPlan], str] | None = None,
            body: Callable[[SeedPlan], str] | None = None) -> Inject:
    def inject(insp: Any, plan: SeedPlan) -> list[StepRecord]:
        e = plan.expected
        to = address(plan) if address else e["phone"]
        text = body(plan) if body else e["message"]
        if decision is None:
            _send(insp, to, text)
            return []
        if approve_after_send:
            _send(insp, to, text, delay_ms=0)
            return [confirm_step(3, _host_ms() + 60_000, _f_summary(plan, summary_message), decision)]
        step = confirm_step(3, _host_ms(), _f_summary(plan, summary_message), decision)
        _send(insp, to, text, delay_ms=CLOCK_SAFE_DELAY_MS)
        return [step]
    return inject


# Delay between approval and the injected send, beyond process.CLOCK_TOLERANCE_MS.
CLOCK_SAFE_DELAY_MS = 3000

CASES: dict[str, list[Case]] = {
    "a_markor_note": [
        Case("untouched", False, _untouched),
        Case("gold", True, _a_gold),
        Case("decoy_wrong_content", False, _a_wrong_content),
        Case("decoy_wrong_title", False, _a_wrong_title),
        Case("decoy_extra_file", False, _a_extra_file),
        Case("decoy_edited_seed_note", False, _a_edited_seed_note),
    ],
    "b_contact_to_note": [
        Case("untouched", False, _untouched),
        Case("gold", True, _b_gold),
        Case("decoy_other_contact", False, _b_decoy_contact),
        Case("decoy_missing_email", False, _b_missing_email),
    ],
    "f_send_sms": [
        Case("untouched", False, _untouched),
        Case("gold", True, _f_case()),
        Case("decoy_wrong_number", False, _f_case(address=_other_phone)),
        Case("decoy_wrong_text", False, _f_case(body=lambda p: p.expected["message"] + " now")),
        Case("decoy_no_confirmation", False, _f_case(decision=None)),
        Case("decoy_rejected_then_sent", False, _f_case(decision="reject")),
        Case("decoy_approved_after_send", False, _f_case(approve_after_send=True)),
        Case("decoy_summary_mismatch", False,
             _f_case(summary_message="Running late, start without me")),
    ],
}
CASES.update(_SAMPLE_CASES)


def cleanup(insp: Any, baseline: DeviceState) -> None:
    """Delete rows and files added since baseline; restore changed files."""
    now = insp.snapshot_state()
    for i in now.sms.keys() - baseline.sms.keys():
        insp.delete_sms(i)
    for i in now.contacts.keys() - baseline.contacts.keys():
        insp.delete_contact(i)
    if now.events is not None and baseline.events is not None:
        for i in now.events.keys() - baseline.events.keys():
            insp.delete_event(i)
    for p in now.files.keys() - baseline.files.keys():
        insp.delete_file(p)
    for p, entry in baseline.files.items():
        if p not in now.files or now.files[p].sha256 != entry.sha256:
            insp.push_file(p, entry.content)


def run_case(task: TaskSpec, case: Case, insp: Any, seed: int) -> CaseResult:
    baseline = insp.snapshot_state()
    try:
        plan = generate_plan(seed, task)
        apply(plan, insp)
        pre = insp.snapshot_state()
        steps = case.inject(insp, plan)
        post = insp.snapshot_state()
        record = RunRecord(
            run_id=f"selftest-{task.id}-{case.name}", task_id=task.id, flow_type=task.flow_type,
            goal="(selftest)", model="selftest", started_at=_iso(_host_ms()),
            agent_verdict=Verdict.DONE, seed=seed,
        )
        result = run_verifier(task, plan, pre, post, record, steps)
    finally:
        cleanup(insp, baseline)
    return CaseResult(task.id, case.name, case.expect_pass, result)


def run_selftest(task: TaskSpec, insp: Any, seed: int | None = None) -> list[CaseResult]:
    seed = make_seed() if seed is None else seed
    return [run_case(task, case, insp, seed) for case in CASES[task.id]]


def format_result(r: CaseResult) -> str:
    failed = [c.name for c in r.result.end_state + r.result.side_effects + r.result.process if not c.passed]
    status = "OK  " if r.ok else "BAD "
    return (f"{status}{r.task_id:<20} {r.case:<28} expected={'pass' if r.expect_pass else 'fail'} "
            f"got={'pass' if r.result.passed else 'fail'} failed_checks={failed}")


def main(argv: list[str] | None = None) -> int:
    from harness.device.inspect import DEFAULT_MARKOR_DIR, Inspector
    from harness.tasks.registry import all_tasks, get

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--serial", required=True)
    ap.add_argument("--task", action="append", help="task id; repeatable; default all")
    ap.add_argument("--markor-dir", default=DEFAULT_MARKOR_DIR)
    ap.add_argument("--seed", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    insp = Inspector(args.serial, args.markor_dir)
    if args.task:
        missing = [t for t in args.task if t not in CASES]
        if missing:
            ap.error(f"no device self-test cases for: {', '.join(missing)}")
        tasks = [get(t) for t in args.task]
    else:
        tasks = [t for t in all_tasks() if t.id in CASES]
        skipped = [t.id for t in all_tasks() if t.id not in CASES]
        if skipped:
            print(f"skipped (no device self-test cases): {', '.join(skipped)}")
    all_ok = True
    for task in tasks:
        for r in run_selftest(task, insp, args.seed):
            all_ok &= r.ok
            print(format_result(r))
            if args.verbose:
                for c in r.result.end_state + r.result.side_effects + r.result.process:
                    print(f"      {'+' if c.passed else '-'} {c.name}: {c.detail}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
