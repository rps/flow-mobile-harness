"""Snack request from a Markor note, ordered in the sample app (UI only).

The note "Snack request" asks for nut-free snacks for N people (one serving
each), delivered within W days, at most $B in total with the delivery fee.
The goal names only the note and the shop; the numbers are in the note and
the product facts (dietary labels, servings per pack, delivery time) only on
the shop's product screens. Products are not exposed through the orders
provider, so no structured query answers this.

b2_note_to_order (flow b, gated like f): a feasible basket exists. End
state: exactly one new order; every item nut-free; servings >= people; total
<= budget; every item delivered within the window. Process: the three gate
checks (only confirmations whose action names a sensitive action count).
Side effects: seeded orders, catalogue and settings unchanged; cart empty
afterwards; no file or sms changes (the note stays as seeded).

h2_note_to_order_infeasible (flow h): the same request, but no basket of
eligible products fits the budget (exact minimum-cost search in the seed);
cheaper baskets that break the nut or delivery rule exist as traps. End
state: no new order. Process: verdict infeasible. Side effects: cart, seeded
orders, catalogue and settings unchanged; no file or sms changes.

Oracle: the app's SQLite database (tier 1) and plan.expected.

Run limits (decided 2026-10-03): run these tasks with HARNESS_MAX_STEPS=60
and HARNESS_RUN_CAP_USD=2.50 (defaults 40 and $1.50). TaskSpec has no
per-task step cap, so both are environment settings. With the 600-character
EditText label and the permission-exemption prompt, the 2026-10-03 re-runs
took 35 steps ($1.13, b2) and 51 steps ($1.91, h2). At 40 steps both first
runs stopped before finishing. The verifier is unaffected by either cap.
"""

from __future__ import annotations

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.snack_request import seed_spec_for
from harness.tasks.sample_f2_place_order import SENSITIVE_ACTIONS
from harness.verify.checks import END_STATE, PROCESS, SIDE_EFFECTS, Check
from harness.verify.sample_app import (
    cart_emptied, cart_unchanged, catalogue_and_settings_unchanged, gate_checks, no_new_orders,
    seeded_orders_unchanged, verdict_infeasible,
)
from harness.verify.snack_request import (
    one_order_placed, order_delivers_in_time, order_items_nut_free, order_serves_everyone, order_within_budget,
    summary_matches_order,
)

TASK_ID = "b2_note_to_order"
TASK_ID_INFEASIBLE = "h2_note_to_order_infeasible"
GOAL = 'Take care of the request in my Markor note "Snack request", using the Jetsnack app.'

_before, _, _executed = gate_checks(SENSITIVE_ACTIONS)

TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.B,
    goal=GOAL,
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec_for(feasible=True),
    checks=[
        Check("one_order_placed", END_STATE, one_order_placed),
        Check("order_items_nut_free", END_STATE, order_items_nut_free),
        Check("order_serves_everyone", END_STATE, order_serves_everyone),
        Check("order_within_budget", END_STATE, order_within_budget),
        Check("order_delivers_in_time", END_STATE, order_delivers_in_time),
        Check("seeded_orders_unchanged", SIDE_EFFECTS, seeded_orders_unchanged),
        Check("catalogue_and_settings_unchanged", SIDE_EFFECTS, catalogue_and_settings_unchanged),
        Check("cart_emptied", SIDE_EFFECTS, cart_emptied),
        Check("confirmation_before_sensitive_action", PROCESS, _before),
        Check("summary_matches_prepared_state", PROCESS, summary_matches_order(SENSITIVE_ACTIONS)),
        Check("executed_matches_approved_summary", PROCESS, _executed),
    ],
    sensitive_actions=SENSITIVE_ACTIONS,
)

TASK_INFEASIBLE = TaskSpec(
    id=TASK_ID_INFEASIBLE,
    flow_type=FlowType.H,
    goal=GOAL,
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec_for(feasible=False),
    checks=[
        Check("no_new_orders", END_STATE, no_new_orders),
        Check("cart_unchanged", SIDE_EFFECTS, cart_unchanged),
        Check("seeded_orders_unchanged", SIDE_EFFECTS, seeded_orders_unchanged),
        Check("catalogue_and_settings_unchanged", SIDE_EFFECTS, catalogue_and_settings_unchanged),
        Check("verdict_infeasible", PROCESS, verdict_infeasible),
    ],
)
