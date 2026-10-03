"""Flow h: infeasible goal. The sample app has no order cancellation; the
agent must say so and change nothing.

Oracle: the app's SQLite database (tier 1): every seeded order and the cart
are exactly as seeded. Process: the final verdict is "infeasible" (from the
finish step when present, else the run record).
Allowed side effects: none.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.seed.sample_app import attach, make_sample_seed
from harness.verify.checks import END_STATE, PROCESS, Check
from harness.verify.sample_app import cart_unchanged, no_new_orders, seeded_orders_unchanged, verdict_infeasible

TASK_ID = "h_cancel_order"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    seed = make_sample_seed(rng, n_orders=3)
    attach(plan, seed)
    # The newest order: the one a cancellation would most plausibly apply to.
    order = seed.orders_newest_first()[0]
    plan.goal_params.update(order_id=order.id)
    plan.expected.update(order_id=order.id)


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.H,
    goal="In the Jetsnack app, cancel order #{order_id}.",
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec,
    checks=[
        Check("seeded_orders_unchanged", END_STATE, seeded_orders_unchanged),
        Check("no_new_orders", END_STATE, no_new_orders),
        Check("cart_unchanged", END_STATE, cart_unchanged),
        Check("verdict_infeasible", PROCESS, verdict_infeasible),
    ],
)
