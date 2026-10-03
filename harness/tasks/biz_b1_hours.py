"""Flow b (business): TimeCamp weekly hours for a client project into a Markor note.

Oracle: tier 5, scripted read-back of TimeCamp's Reports tab through
BusinessInspector (no root, no API token). The expected value is not seeded:
it is the project's "This Week" total as TimeCamp shows it before the run.
Normalisation: note matched by title (see task a); hours may be written as
4.25, "4h 15m", "4 hours 15 minutes" or "4:15".
Allowed side effects: one added file; nothing else.
Snapshot `business` holds project "Northwind Traders" (4h 15m this week)
and the decoys "Android Flow" (2h) and "Ideation" (5h).
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.verify.biz_checks import note_exists, note_has_week_hours
from harness.verify.biz_readback import CLIENT_ORG
from harness.verify.checks import END_STATE, Check
from harness.verify.diff import AllowedChanges

TASK_ID = "biz_b1_hours"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    title = f"{CLIENT_ORG} hours {rng.randrange(10, 100)}"
    plan.goal_params.update(client=CLIENT_ORG, title=title)
    plan.expected.update(client=CLIENT_ORG, title=title)


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.B,
    goal='Open the TimeCamp app and find the total time tracked this week on the project "{client}". '
         'Write that total in a new Markor note titled "{title}".',
    oracle_tier=OracleTier.SCRIPTED_READBACK,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, note_exists),
        Check("note_has_week_hours", END_STATE, note_has_week_hours),
        AllowedChanges(files_added=1),
    ],
)
