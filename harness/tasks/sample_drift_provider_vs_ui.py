"""Flow drift: the orders provider is seeded stale (newest order omitted);
the agent must compare it with the app's order list and report the mismatch
in a Markor note: first line MATCH or MISMATCH, then the order numbers found
in only one source.

Oracle: Markor's files (tier 1); the missing order's id comes from the seed.
Normalisation: the first MATCH/MISMATCH word on the first non-empty line (a
repeated title skipped; "1. MISMATCH", "Result: MISMATCH", "**MISMATCH**" all
count), casefolded;
order id as digits not adjacent to other digits. The second-newest order (the
one the provider still shows as newest) is the decoy: naming it fails.
Allowed side effects: one added file; the app's orders and cart unchanged.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.seed.sample_app import QUERY_NAME, attach, make_sample_seed
from harness.verify.checks import END_STATE, SIDE_EFFECTS, Check, VerifyContext, new_note_titled
from harness.verify.diff import AllowedChanges
from harness.verify.sample_app import cart_unchanged, no_new_orders, order_id_in_text, seeded_orders_unchanged, verdict_word

TASK_ID = "drift_provider_vs_ui"
MISMATCH = "mismatch"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    seed = make_sample_seed(rng, n_orders=3, provider_mode="stale")
    attach(plan, seed)
    newest = seed.orders_newest_first()
    title = f"Order audit {rng.randrange(10, 100)}"
    plan.goal_params.update(title=title, query=QUERY_NAME)
    plan.expected.update(title=title, missing_id=newest[0].id, decoy_id=newest[1].id)


def _note_exists(ctx: VerifyContext) -> tuple[bool, str]:
    path, _, detail = new_note_titled(ctx, ctx.expected["title"])
    return path is not None, detail


def _note_says_mismatch(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    word = verdict_word(text, ctx.expected["title"])
    if word == MISMATCH:
        return True, "first line says MISMATCH"
    return False, f"first line verdict {word!r}"


def _note_names_missing_order(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    if not order_id_in_text(text, ctx.expected["missing_id"]):
        return False, f"order {ctx.expected['missing_id']} not named"
    if order_id_in_text(text, ctx.expected["decoy_id"]):
        return False, f"order {ctx.expected['decoy_id']} is in both sources but is named"
    return True, f"order {ctx.expected['missing_id']} named"


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.DRIFT,
    goal='Compare the orders listed in the Jetsnack app with the orders returned by the {query} data query. '
         'Save a Markor note titled "{title}" whose first line is MATCH if both show the same orders or '
         'MISMATCH if they differ, followed by the order numbers that appear in only one of them.',
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, _note_exists),
        Check("note_says_mismatch", END_STATE, _note_says_mismatch),
        Check("note_names_missing_order", END_STATE, _note_names_missing_order),
        Check("seeded_orders_unchanged", SIDE_EFFECTS, seeded_orders_unchanged),
        Check("no_new_orders", SIDE_EFFECTS, no_new_orders),
        Check("cart_unchanged", SIDE_EFFECTS, cart_unchanged),
        AllowedChanges(files_added=1),
    ],
)
