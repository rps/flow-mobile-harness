"""Flow d: multi-app orchestration. Two latest orders from the sample app
into a Markor note, then an unsent text to a seeded contact.

Oracle: the app's SQLite database for the orders (tier 1), Markor's files for
the note, the sms provider for "nothing sent".
Normalisation: note matched by title as in task a; order numbers as digits
not adjacent to other digits; totals as dollars with two decimals, "$"
optional. The third-newest order (id and total) is the decoy: listing it fails.
Allowed side effects: one added file; no outgoing sms; at most one draft row,
and only to the seeded contact (Google Messages keeps drafts in its own
store, so on the stock image none appears in the sms provider); the app's
orders and cart unchanged. Pre-existing drafts may vanish.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.seed.sample_app import attach, make_sample_seed
from harness.verify.checks import END_STATE, SIDE_EFFECTS, Check, VerifyContext, new_note_titled
from harness.verify.diff import AllowedChanges
from harness.verify.sample_app import (
    cart_unchanged, money_in_text, new_drafts_only_to_contact, no_new_orders, nothing_sent, order_id_in_text,
    seeded_orders_unchanged,
)

TASK_ID = "d_orders_to_note_to_message"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    seed = make_sample_seed(rng, n_orders=3)
    attach(plan, seed)
    newest = seed.orders_newest_first()
    target = rng.choice(plan.contacts)
    title = f"Recent orders {rng.randrange(10, 100)}"
    plan.goal_params.update(title=title, contact_name=target.name)
    plan.expected.update(
        title=title,
        orders=[(o.id, o.total_cents) for o in newest[:2]],
        decoy_id=newest[2].id,
        decoy_total=newest[2].total_cents,
        name=target.name,
        phone=target.phone,
    )


def _note_exists(ctx: VerifyContext) -> tuple[bool, str]:
    path, _, detail = new_note_titled(ctx, ctx.expected["title"])
    return path is not None, detail


def _note_has_order_numbers(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    missing = [oid for oid, _ in ctx.expected["orders"] if not order_id_in_text(text, oid)]
    if missing:
        return False, f"order numbers missing: {missing}"
    if order_id_in_text(text, ctx.expected["decoy_id"]):
        return False, f"note also lists older order {ctx.expected['decoy_id']}"
    return True, "both order numbers found"


def _note_has_totals(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    missing = [total for _, total in ctx.expected["orders"] if not money_in_text(text, total)]
    if missing:
        return False, f"totals missing (cents): {missing}"
    if money_in_text(text, ctx.expected["decoy_total"]):
        return False, "note also gives an older order's total"
    return True, "both totals found"


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.D,
    goal='Open the Jetsnack app and find my two most recent orders. Save a Markor note titled '
         '"{title}" with each order number and its total. Then, in Messages, write a text to '
         '{contact_name} with the two totals but do not send it: leave the message unsent.',
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, _note_exists),
        Check("note_has_both_order_numbers", END_STATE, _note_has_order_numbers),
        Check("note_has_both_totals", END_STATE, _note_has_totals),
        Check("nothing_sent", END_STATE, nothing_sent),
        Check("new_drafts_only_to_contact", SIDE_EFFECTS, new_drafts_only_to_contact),
        Check("seeded_orders_unchanged", SIDE_EFFECTS, seeded_orders_unchanged),
        Check("no_new_orders", SIDE_EFFECTS, no_new_orders),
        Check("cart_unchanged", SIDE_EFFECTS, cart_unchanged),
        AllowedChanges(files_added=1, drafts_may_vanish=True, drafts_added=1),
    ],
)
