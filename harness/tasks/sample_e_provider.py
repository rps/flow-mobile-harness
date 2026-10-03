"""Flow e: hybrid routing. "Total of my most recent order" into a Markor
note, once with the orders provider complete (e_provider_full) and once with
it stale (e_provider_fallback: the provider omits the newest order, so the
structured answer is the previous order's total and only the UI is right).

Oracle: Markor's files for the answer (tier 1); the expected total comes from
the seed. Normalisation: dollars with two decimals, "$" optional. The note
fails if it also gives the previous order's total (the stale answer).
Allowed side effects: one added file; the app's orders and cart unchanged.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import SeedPlan
from harness.seed.sample_app import attach, make_sample_seed
from harness.verify.checks import END_STATE, SIDE_EFFECTS, Check, VerifyContext, new_note_titled
from harness.verify.diff import AllowedChanges
from harness.verify.sample_app import cart_unchanged, money_in_text, no_new_orders, seeded_orders_unchanged

STALE_ANSWER = "note gives the previous order's total, not the newest (the stale provider's answer)"
TOTAL_CHECK = "note_has_newest_order_total"

GOAL = 'Find the total of my most recent order in the Jetsnack app and save it in a Markor note titled "{title}".'


def make_seed_spec(provider_mode: str):
    def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
        seed = make_sample_seed(rng, n_orders=3, provider_mode=provider_mode)
        attach(plan, seed)
        newest = seed.orders_newest_first()
        title = f"Last order total {rng.randrange(10, 100)}"
        plan.goal_params.update(title=title)
        plan.expected.update(title=title, total=newest[0].total_cents, decoy_total=newest[1].total_cents,
                             provider_mode=provider_mode)
    return seed_spec


def _note_exists(ctx: VerifyContext) -> tuple[bool, str]:
    path, _, detail = new_note_titled(ctx, ctx.expected["title"])
    return path is not None, detail


def _note_has_total(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    if not money_in_text(text, ctx.expected["total"]):
        if money_in_text(text, ctx.expected["decoy_total"]):
            return False, STALE_ANSWER
        return False, "newest order's total missing"
    if money_in_text(text, ctx.expected["decoy_total"]):
        return False, "note also gives the previous order's total"
    return True, "total found"


def total_task(task_id: str, provider_mode: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        flow_type=FlowType.E,
        goal=GOAL,
        oracle_tier=OracleTier.OWN_STORAGE,
        seed_spec=make_seed_spec(provider_mode),
        checks=[
            Check("note_created_with_title", END_STATE, _note_exists),
            Check(TOTAL_CHECK, END_STATE, _note_has_total),
            Check("seeded_orders_unchanged", SIDE_EFFECTS, seeded_orders_unchanged),
            Check("no_new_orders", SIDE_EFFECTS, no_new_orders),
            Check("cart_unchanged", SIDE_EFFECTS, cart_unchanged),
            AllowedChanges(files_added=1),
        ],
    )


TASK_FULL = total_task("e_provider_full", "full")
TASK_FALLBACK = total_task("e_provider_fallback", "stale")


def designed_failure(task_id: str | None, result) -> bool:
    """The failure e_provider_fallback is built to show: the agent saved the
    stale provider's answer and nothing else is wrong. Any other failure of
    that task (no note, a wrong figure, a side effect) is not."""
    if task_id != TASK_FALLBACK.id or result is None or result.passed:
        return False
    failed = [c for c in (*result.end_state, *result.side_effects, *result.process) if not c.passed]
    return [(c.name, c.detail) for c in failed] == [(TOTAL_CHECK, STALE_ANSWER)]
