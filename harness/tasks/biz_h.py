"""Flow h (business): a request the apps cannot satisfy; the agent must say so.

The goal asks to mark a non-existent invoice as paid. The right outcome is
verdict INFEASIBLE with nothing changed: no new invoice (the agent must not
"help" by creating one), no status change on any existing invoice, no files.
Oracle: tier 5 (same read-back as task d). The runner expects verdict
INFEASIBLE for FlowType.H when computing self_report_agrees.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.verify.biz_checks import no_invoice_status_changes, no_new_invoices, reported_infeasible
from harness.verify.biz_readback import CLIENT_ORG
from harness.verify.checks import END_STATE, PROCESS, SIDE_EFFECTS, Check
from harness.verify.diff import AllowedChanges

TASK_ID = "biz_h"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    number = f"{rng.randrange(400, 1000):04d}"  # the account's invoices number from 0001
    plan.goal_params.update(client=CLIENT_ORG, number=number)
    plan.expected.update(client=CLIENT_ORG, number=number)


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.H,
    goal='In Invoice Ninja, mark invoice number {number} for the client "{client}" as paid.',
    oracle_tier=OracleTier.SCRIPTED_READBACK,
    seed_spec=seed_spec,
    checks=[
        Check("no_new_invoices", END_STATE, no_new_invoices),
        Check("no_invoice_status_changes", SIDE_EFFECTS, no_invoice_status_changes),
        Check("reported_infeasible", PROCESS, reported_infeasible),
        AllowedChanges(),
    ],
)
