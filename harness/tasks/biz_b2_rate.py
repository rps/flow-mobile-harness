"""Flow b (business): a client's hourly rate from Insightly CRM into a Markor note.

Oracle: tier 5, scripted read-back of the Insightly organisation record
through BusinessInspector. The rate lives in the organisation's Description
field ("Hourly rate: 95 USD per hour"); the mobile app cannot define custom
fields, see apps-probe/PROBE.md. Not seeded: the expected rate is what
Insightly shows before the run.
Normalisation: note matched by title; the rate may be written as 95, 95.00
or $95. Decoys on the snapshot: organisation "Northwind Logistics" (80) and a
stray note titled "hourly rate" with body "$175/hr".
Allowed side effects: one added file; nothing else.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.verify.biz_checks import note_exists, note_has_rate
from harness.verify.biz_readback import CLIENT_ORG
from harness.verify.checks import END_STATE, Check
from harness.verify.diff import AllowedChanges

TASK_ID = "biz_b2_rate"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    title = f"{CLIENT_ORG} rate {rng.randrange(10, 100)}"
    plan.goal_params.update(client=CLIENT_ORG, title=title)
    plan.expected.update(client=CLIENT_ORG, title=title)


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.B,
    goal='In the Insightly CRM app, open the organization "{client}" and find its hourly rate. '
         'Save the rate in a new Markor note titled "{title}".',
    oracle_tier=OracleTier.SCRIPTED_READBACK,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, note_exists),
        Check("note_has_rate", END_STATE, note_has_rate),
        AllowedChanges(files_added=1),
    ],
)
