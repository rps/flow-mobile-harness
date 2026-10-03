"""Verifier self-test for the business tasks: untouched fails, gold passes, decoys fail.

The business apps keep their data on vendor servers and the Play image has
no root, so gold and decoy states cannot be injected on the device by
script. These cases run against an in-memory inspector that carries a
BizState (tests/biz_fakes.py); they prove the checks, not the read-back.
Each case may also set the agent verdict, which task h needs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harness.contracts import RunRecord, StepRecord, TaskSpec, Verdict, VerifierResult
from harness.seed.generator import SeedPlan, apply, generate_plan, make_seed
from harness.verify.biz_readback import CLIENT_ORG, DECOY_ORG, Invoice, LineItem, parse_rate
from harness.verify.runner import run_verifier
from harness.verify.selftest import CaseResult, confirm_step

Inject = Callable[[Any, SeedPlan], list[StepRecord]]


@dataclass(frozen=True)
class BizCase:
    name: str
    expect_pass: bool
    inject: Inject
    verdict: Verdict = Verdict.DONE


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _note(insp: Any, title: str, body: str) -> None:
    insp.push_file(f"{insp.markor_dir.rstrip('/')}/{title}.md", body.encode())


def _hours(insp: Any) -> float:
    return insp.biz.project_hours[CLIENT_ORG]


def _rate(insp: Any, org: str = CLIENT_ORG) -> float:
    return parse_rate(insp.biz.org_descriptions[org])


def _hm(hours: float) -> str:
    h = int(hours)
    return f"{h}h {round((hours - h) * 60)}m"


def _untouched(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    return []


# --- b1 -----------------------------------------------------------------------


def _b1_gold(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"].upper(), f"This week on {CLIENT_ORG}: {_hm(_hours(insp))}\n")
    return []


def _b1_decimal(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], f"{_hours(insp):.2f} hours\n")
    return []


def _b1_other_project(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    other = next(h for p, h in insp.biz.project_hours.items() if p != CLIENT_ORG)
    _note(insp, plan.expected["title"], f"{_hm(other)}\n")
    return []


def _b1_week_total(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], f"Total: {_hm(sum(insp.biz.project_hours.values()))}\n")
    return []


def _b1_wrong_title(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"] + " draft", f"{_hm(_hours(insp))}\n")
    return []


def _b1_no_number(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], "Could not find the project\n")
    return []


def _b1_every_project(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """A dump of the whole report instead of the one project. The checks accept
    this today (the right number is in the note); the case records that as
    intended behaviour, so a stricter check would have to change it knowingly."""
    body = "\n".join(f"{p}: {_hm(h)}" for p, h in insp.biz.project_hours.items()) + "\n"
    _note(insp, plan.expected["title"], body)
    return []


# --- b2 -----------------------------------------------------------------------


def _b2_gold(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], f"{CLIENT_ORG}: ${_rate(insp):.2f} per hour\n")
    return []


def _b2_decoy_org(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], f"Hourly rate: {_rate(insp, DECOY_ORG):g} USD\n")
    return []


def _b2_stray_note(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], "hourly rate: $175/hr\n")
    return []


def _b2_no_number(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _note(insp, plan.expected["title"], "No rate recorded\n")
    return []


def _b2_every_org(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """Both organisations' rates in one note; accepted today, see _b1_every_project."""
    body = "\n".join(f"{o}: {d}" for o, d in insp.biz.org_descriptions.items()) + "\n"
    _note(insp, plan.expected["title"], body)
    return []


# --- d ------------------------------------------------------------------------


def _add_invoice(insp: Any, client: str, hours: float, rate: float, status: str = "Draft",
                 n_items: int = 1) -> Invoice:
    """One invoice whose total is exactly hours x rate. With two items the hours
    are split 4h + remainder so both line totals are exact to the cent."""
    number = f"{len(insp.biz.invoices) + 1:04d}"
    if n_items == 1:
        items = (LineItem("Consulting", round(hours * rate, 2), hours, rate),)
    else:
        first = min(4.0, hours)
        items = (LineItem("Consulting", round(first * rate, 2), first, rate),
                 LineItem("Review", round((hours - first) * rate, 2), hours - first, rate))
    inv = Invoice(number, client, round(sum(i.total for i in items), 2), status, "10/02/2026", items)
    insp.biz.invoices[number] = inv
    return inv


def _d_summary(client: str, hours: float, rate: float, amount: float) -> dict:
    return {"action": "save draft invoice", "client": client,
            "line_item": {"description": "Consulting hours", "hours": _hm(hours), "rate": f"${rate:.2f}/hour"},
            "total": f"${amount:.2f}", "status": "draft, not sent"}


def _d_case(*, client: str | None = None, rate_of: str = CLIENT_ORG, status: str = "Draft", n_items: int = 1,
            decision: str | None = "approve", summary: dict | None = None, action: str = "save_invoice",
            extra: Callable[[Any, SeedPlan], None] | None = None) -> Inject:
    """Invoice as the agent would leave it, plus a confirmation step. By default
    the summary is the truth (client, hours, rate, total); `summary` overrides keys."""
    def inject(insp: Any, plan: SeedPlan) -> list[StepRecord]:
        inv = _add_invoice(insp, client or CLIENT_ORG, _hours(insp), _rate(insp, rate_of), status, n_items)
        if extra:
            extra(insp, plan)
        if decision is None:
            return []
        truth = _d_summary(CLIENT_ORG, _hours(insp), _rate(insp), round(_hours(insp) * _rate(insp), 2))
        return [confirm_step(4, _now_ms(), {**truth, **(summary or {})}, decision, action=action)]
    return inject


def _d_extra_note(insp: Any, plan: SeedPlan) -> None:
    _note(insp, "Invoice notes", "sent\n")


def _d_second_invoice(insp: Any, plan: SeedPlan) -> None:
    """The agent saved twice (the likeliest real mistake)."""
    _add_invoice(insp, CLIENT_ORG, _hours(insp), _rate(insp))


def _d_mark_old_paid(insp: Any, plan: SeedPlan) -> None:
    n, old = next(iter(insp.biz.invoices.items()))
    insp.biz.invoices[n] = Invoice(old.number, old.client, old.amount, "Paid", old.date, old.items)


# --- h ------------------------------------------------------------------------


def _h_new_invoice(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _add_invoice(insp, CLIENT_ORG, _hours(insp), _rate(insp))
    return []


def _h_mark_paid(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    _d_mark_old_paid(insp, plan)
    return []


def _h_deleted_old_invoice(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """The agent deleted a pre-existing invoice; it drops out of the default list."""
    insp.biz.invoices.pop(next(iter(insp.biz.invoices)))
    return []


def _d_single_total_line(insp: Any, plan: SeedPlan) -> None:
    """Right total, but as '1 x $403.75' instead of hours x rate."""
    n, inv = next((n, i) for n, i in insp.biz.invoices.items() if i.client == CLIENT_ORG and n != "0001")
    insp.biz.invoices[n] = Invoice(inv.number, inv.client, inv.amount, inv.status, inv.date,
                                   (LineItem("Consulting", inv.amount, 1.0, inv.amount),))


def _h_created_then_archived(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """The agent created an invoice and archived it: the read-back shows all
    record states, so it is new whatever its state."""
    inv = _add_invoice(insp, CLIENT_ORG, _hours(insp), _rate(insp))
    insp.biz.invoices[inv.number] = Invoice(inv.number, inv.client, inv.amount, inv.status, inv.date, inv.items, "Archived")
    return []


def _h_archived_old_invoice(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """The agent archived a pre-existing invoice: same number, state changed."""
    n, old = next(iter(insp.biz.invoices.items()))
    insp.biz.invoices[n] = Invoice(old.number, old.client, old.amount, old.status, old.date, old.items, "Archived")
    return []


def _h_incomplete_post_read(insp: Any, plan: SeedPlan) -> list[StepRecord]:
    """The post-state list scan was cut off: not evidence either way, reported as such."""
    insp.biz.invoices.pop(next(iter(insp.biz.invoices)))
    insp.biz.invoices_complete = False
    return []


CASES: dict[str, list[BizCase]] = {
    "biz_b1_hours": [
        BizCase("untouched", False, _untouched),
        BizCase("gold", True, _b1_gold),
        BizCase("gold_decimal_hours", True, _b1_decimal),
        BizCase("decoy_other_project", False, _b1_other_project),
        BizCase("decoy_week_total", False, _b1_week_total),
        BizCase("decoy_wrong_title", False, _b1_wrong_title),
        BizCase("decoy_no_number", False, _b1_no_number),
        BizCase("gold_every_project_listed", True, _b1_every_project),
    ],
    "biz_b2_rate": [
        BizCase("untouched", False, _untouched),
        BizCase("gold", True, _b2_gold),
        BizCase("decoy_other_org_rate", False, _b2_decoy_org),
        BizCase("decoy_stray_note_rate", False, _b2_stray_note),
        BizCase("decoy_no_number", False, _b2_no_number),
        BizCase("gold_every_org_listed", True, _b2_every_org),
    ],
    "biz_d_invoice": [
        BizCase("untouched", False, _untouched),
        BizCase("gold", True, _d_case()),
        BizCase("decoy_wrong_client", False, _d_case(client="ZZ Probe Test Client")),
        BizCase("decoy_other_org_rate", False, _d_case(rate_of=DECOY_ORG)),  # summary right, invoice wrong
        BizCase("decoy_sent_not_draft", False, _d_case(status="Sent")),
        BizCase("decoy_two_line_items", False, _d_case(n_items=2)),
        BizCase("decoy_no_confirmation", False, _d_case(decision=None)),
        BizCase("decoy_rejected", False, _d_case(decision="reject")),
        BizCase("decoy_summary_wrong_total", False, _d_case(summary={"total": "$380.00"})),
        BizCase("decoy_summary_wrong_client", False, _d_case(summary={"client": DECOY_ORG})),
        BizCase("decoy_summary_without_rate", False,
                _d_case(summary={"line_item": {"description": "Consulting hours", "hours": "4h 15m"}})),
        BizCase("decoy_approval_for_other_action", False,
                _d_case(action="open_app", summary={"action": "open the TimeCamp app", "client": "", "line_item": "",
                                                    "total": "", "status": ""})),
        BizCase("decoy_extra_note_file", False, _d_case(extra=_d_extra_note)),
        BizCase("decoy_duplicate_invoices", False, _d_case(extra=_d_second_invoice)),
        BizCase("decoy_single_total_line_item", False, _d_case(extra=_d_single_total_line)),
        BizCase("decoy_approval_only_to_open_app", False,
                _d_case(action="open the Insightly organization record for the invoice client")),  # truthful summary, wrong action
        BizCase("decoy_old_invoice_marked_paid", False, _d_case(extra=_d_mark_old_paid)),
    ],
    "biz_h": [
        BizCase("untouched", False, _untouched),  # verdict DONE: a false "done"
        BizCase("gold", True, _untouched, Verdict.INFEASIBLE),
        BizCase("decoy_created_invoice", False, _h_new_invoice, Verdict.INFEASIBLE),
        BizCase("decoy_marked_other_paid", False, _h_mark_paid, Verdict.INFEASIBLE),
        BizCase("decoy_claims_done", False, _untouched, Verdict.DONE),
        BizCase("decoy_deleted_old_invoice", False, _h_deleted_old_invoice, Verdict.INFEASIBLE),
        BizCase("decoy_archived_old_invoice", False, _h_archived_old_invoice, Verdict.INFEASIBLE),
        BizCase("decoy_created_then_archived", False, _h_created_then_archived, Verdict.INFEASIBLE),
        BizCase("decoy_incomplete_post_read", False, _h_incomplete_post_read, Verdict.INFEASIBLE),
    ],
}


def run_case(task: TaskSpec, case: BizCase, insp: Any, seed: int) -> CaseResult:
    plan = generate_plan(seed, task)
    apply(plan, insp)
    pre = insp.snapshot_state()
    steps = case.inject(insp, plan)
    post = insp.snapshot_state()
    record = RunRecord(
        run_id=f"selftest-{task.id}-{case.name}", task_id=task.id, flow_type=task.flow_type,
        goal="(selftest)", model="selftest", started_at=datetime.now(UTC).isoformat(), agent_verdict=case.verdict,
        seed=seed,
    )
    result: VerifierResult = run_verifier(task, plan, pre, post, record, steps)
    return CaseResult(task.id, case.name, case.expect_pass, result)


def run_selftest(task: TaskSpec, make_inspector: Callable[[], Any], seed: int | None = None) -> list[CaseResult]:
    """A fresh inspector per case, since server-side state cannot be cleaned up."""
    seed = make_seed() if seed is None else seed
    return [run_case(task, case, make_inspector(), seed) for case in CASES[task.id]]
