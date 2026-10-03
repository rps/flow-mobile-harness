"""Per-run data for the snack-request tasks (b2_note_to_order, h2_note_to_order_infeasible).

A Markor note "Snack request.md" asks for snacks for `people` people, one
serving each, nut-free only, delivered within `window_days`, total order cost
(delivery fee included) at most `budget_cents`. The sample app's catalogue (8 products) is
built around those numbers from named roles:

- target: nut-free, on time, a few servings per pack; enough packs fit the
  budget (feasible variant) or just miss it (infeasible variant);
- expensive: nut-free, on time, one pack serves everyone, but it alone is over budget;
- single: nut-free, on time, one serving per pack; enough of them is over budget;
- nuts: contains nuts, on time, and the cheapest way to feed everyone;
- nut_single: contains nuts, on time, one cheap serving; in the feasible
  variant one of it still fits next to the gold basket (a per-line decoy);
- late: nut-free and cheap, but delivered a day or more too late;
- two fillers that break a rule anyway (nuts and late; late).

Feasibility is decided by an exact minimum-cost search over the eligible
products (any quantities), not by the roles, and the generator retries until
the variant holds. Everything the verifier needs stays in plan.expected; the
note carries only the request.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from harness.seed.generator import FIRST_NAMES, SeedNote, SeedPlan
from harness.seed.sample_app import (
    DEFAULT_SHIPPING_CENTS, FIRST_ORDER_ID, ORDERS_BASE_MS, SampleSeed, SeedOrder, SeedOrderItem, SeedProduct, attach,
)

NUT_FREE = "nut-free"
CONTAINS_NUTS = "contains-nuts"
NOTE_TITLE = "Snack request"
NOTE_FILENAME = f"{NOTE_TITLE}.md"
MAX_TRIES = 200

# (name, tagline, bundled image). Names are pairwise not substrings of each other.
NUTTY = [
    ("Almond Trail Mix", "Roasted, lightly salted", "almonds"),
    ("Mixed Nut Tub", "Cashews and pecans", "nuts"),
    ("Hazelnut Nougat", "Honey and hazelnut", "nougat"),
    ("Peanut Butter Cups", "Milk chocolate", "desserts"),
]
NUT_FREE_NAMES = [
    ("Pretzel Twists", "Sea salt", "pretzels"),
    ("Popcorn Share Bag", "Lightly salted", "popcorn"),
    ("Apple Chips", "Baked, no added sugar", "apple_chips"),
    ("Fruit Cups", "Peach and pear", "fruit"),
    ("Dried Mango", "Sweet slices", "mango"),
    ("Mini Cupcakes", "Vanilla", "cupcake"),
    ("Glazed Donuts", "Classic ring", "donut"),
    ("Cheese Crackers", "Aged cheddar", "cheese"),
    ("Kettle Chips", "Sea salt", "chips"),
    ("Marshmallow Bites", "Toasted", "marshmallow"),
    ("Jelly Beans", "Assorted", "jelly_bean"),
    ("Gingerbread Men", "Spiced", "gingerbread"),
]
COLLECTIONS = ("Popular this week", "Office snacks")


@dataclass(frozen=True)
class SnackRequest:
    people: int
    budget_cents: int
    window_days: int


@dataclass(frozen=True)
class SnackRequestSeed:
    request: SnackRequest
    sample: SampleSeed
    roles: dict[str, int]  # role -> product id
    requester: str

    @property
    def note(self) -> SeedNote:
        return SeedNote(NOTE_FILENAME, note_text(self.request, self.requester))


def note_text(req: SnackRequest, requester: str) -> str:
    days = "1 day" if req.window_days == 1 else f"{req.window_days} days"
    return (
        f"# {NOTE_TITLE}\n\n"
        f"From {requester}:\n\n"
        "Could you order snacks for Thursday's planning session? A few things to keep in mind:\n\n"
        f"- We'll be {req.people} people, and everyone should get one serving.\n"
        "- One of us has a nut allergy, so only nut-free snacks, please.\n"
        f"- They need to be delivered within {days}.\n"
        f"- Please don't spend more than ${req.budget_cents // 100}.{req.budget_cents % 100:02d} in total, "
        "delivery fee included.\n\n"
        "Order from Jetsnack, our test shop.\n"
    )


def eligible(p: SeedProduct, req: SnackRequest) -> bool:
    """Nut-free by tag and deliverable within the window."""
    return NUT_FREE in p.tags and p.delivery_days is not None and p.delivery_days <= req.window_days


def basket_cost(basket: dict[int, int], products: dict[int, SeedProduct], shipping: int) -> int:
    return sum(products[pid].price_cents * q for pid, q in basket.items()) + shipping


def basket_servings(basket: dict[int, int], products: dict[int, SeedProduct]) -> int:
    return sum((products[pid].serving_size or 0) * q for pid, q in basket.items())


def cheapest_basket(candidates: list[SeedProduct], people: int) -> tuple[int, dict[int, int]] | None:
    """Minimum subtotal (cents) and quantities over `candidates` reaching at
    least `people` servings (unbounded knapsack, exact). None if no candidate
    has servings."""
    usable = [p for p in candidates if p.serving_size]
    if not usable:
        return None
    best: list[tuple[int, dict[int, int]] | None] = [(0, {})] + [None] * people
    for need in range(1, people + 1):
        for p in usable:
            prev = best[max(0, need - p.serving_size)]  # type: ignore[operator]
            if prev is None:
                continue
            cost = prev[0] + p.price_cents
            if best[need] is None or cost < best[need][0]:  # type: ignore[index]
                basket = dict(prev[1])
                basket[p.id] = basket.get(p.id, 0) + 1
                best[need] = (cost, basket)
    return best[people]


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _attempt(rng: random.Random, feasible: bool) -> SnackRequestSeed | None:
    people = rng.choice([6, 8, 10])
    window = rng.choice([1, 2])
    budget = rng.choice(range(2500, 4501, 500))
    ship = DEFAULT_SHIPPING_CENTS
    room = budget - ship
    nut_free_names = rng.sample(NUT_FREE_NAMES, 6)
    nutty = rng.sample(NUTTY, 3)

    s_target = rng.choice([3, 4, 5])
    n_target = _ceil_div(people, s_target)
    if feasible:
        p_target = (room - rng.randrange(50, 600)) // n_target
    else:
        p_target = (room + rng.randrange(50, 500)) // n_target + 1
    s_expensive = people + rng.randrange(0, 5)
    p_expensive = room + rng.randrange(50, 400)
    p_single = room // people + rng.randrange(30, 120)
    s_nuts = rng.choice([people // 2, people, people + 2])
    p_nuts = max(149, (room - rng.randrange(300, 900)) // _ceil_div(people, s_nuts))
    s_late = rng.choice([people // 2 + 1, people])
    p_late = max(149, (room - rng.randrange(300, 900)) // _ceil_div(people, s_late))
    late_days = rng.randrange(window + 1, 4)

    specs: list[tuple[str, tuple[str, str, str], tuple[str, ...], int, int, int]] = [
        # role, (name, tagline, image), tags, serving_size, delivery_days, price
        ("target", nut_free_names[0], (NUT_FREE,) + (("vegan",) if rng.random() < 0.5 else ()), s_target,
         rng.randrange(1, window + 1), p_target),
        ("expensive", nut_free_names[1], (NUT_FREE, "gluten-free"), s_expensive, rng.randrange(1, window + 1),
         p_expensive),
        ("single", nut_free_names[2], (NUT_FREE,), 1, rng.randrange(1, window + 1), p_single),
        ("nuts", nutty[0], (CONTAINS_NUTS,), s_nuts, rng.randrange(1, window + 1), p_nuts),
        ("late", nut_free_names[3], (NUT_FREE, "vegan"), s_late, late_days, p_late),
        ("filler_nuts", nutty[1], ("vegan", CONTAINS_NUTS), rng.choice([4, 6]), 3, rng.randrange(399, 899)),
        ("nut_single", nutty[2], (CONTAINS_NUTS,), 1, rng.randrange(1, window + 1), rng.randrange(149, 250)),
        ("filler_late", nut_free_names[4], (NUT_FREE, "gluten-free"), rng.choice([2, 6]), 3, rng.randrange(299, 799)),
    ]
    order = list(range(len(specs)))
    rng.shuffle(order)
    products: list[SeedProduct] = []
    roles: dict[str, int] = {}
    for pos, i in enumerate(order):
        role, (name, tagline, image), tags, size, days, price = specs[i]
        pid = 101 + pos
        products.append(SeedProduct(pid, name, tagline, price, image, COLLECTIONS[0] if pos < 3 else COLLECTIONS[1],
                                    tags, size, days))
        roles[role] = pid

    req = SnackRequest(people, budget, window)
    by_id = {p.id: p for p in products}
    best = cheapest_basket([p for p in products if eligible(p, req)], people)
    if best is None or (best[0] + ship <= budget) != feasible:
        return None
    # Decoys must stay what they are meant to be.
    nuts_basket = {roles["nuts"]: _ceil_div(people, s_nuts)}
    late_basket = {roles["late"]: _ceil_div(people, s_late)}
    if basket_cost(nuts_basket, by_id, ship) > budget or basket_cost(late_basket, by_id, ship) > budget:
        return None
    if basket_cost({roles["expensive"]: 1}, by_id, ship) <= budget:
        return None
    if feasible and basket_cost({roles["target"]: n_target, roles["nut_single"]: 1}, by_id, ship) > budget:
        return None

    orders = _past_orders(rng, products)
    sample = SampleSeed(tuple(products), (), tuple(orders), "A", "full", ship)
    return SnackRequestSeed(req, sample, roles, rng.choice(FIRST_NAMES))


def _past_orders(rng: random.Random, products: list[SeedProduct]) -> list[SeedOrder]:
    placed = ORDERS_BASE_MS + rng.randrange(0, 5) * 86_400_000
    orders = []
    for n in range(2):
        chosen = rng.sample(products, rng.randrange(1, 3))
        items = tuple(SeedOrderItem(p.id, p.name, rng.randrange(1, 3), p.price_cents) for p in chosen)
        orders.append(SeedOrder(FIRST_ORDER_ID + n, placed, "DELIVERED", DEFAULT_SHIPPING_CENTS, items))
        placed += rng.randrange(3, 9) * 86_400_000
    return orders


def make_snack_request(rng: random.Random, feasible: bool) -> SnackRequestSeed:
    for _ in range(MAX_TRIES):
        made = _attempt(rng, feasible)
        if made is not None:
            return made
    raise RuntimeError(f"no {'feasible' if feasible else 'infeasible'} snack request after {MAX_TRIES} tries")


def expected_for(made: SnackRequestSeed) -> dict[str, Any]:
    """Verifier-only values: the request, the cheapest eligible basket, and
    single-cause decoy baskets for the self-tests."""
    req, sample, roles = made.request, made.sample, made.roles
    by_id = {p.id: p for p in sample.products}
    best = cheapest_basket([p for p in sample.products if eligible(p, req)], req.people)
    assert best is not None
    target = by_id[roles["target"]]
    n_target = _ceil_div(req.people, target.serving_size or 1)

    def need(role: str) -> dict[int, int]:
        p = by_id[roles[role]]
        return {p.id: _ceil_div(req.people, p.serving_size or 1)}

    return {
        "people": req.people,
        "budget_cents": req.budget_cents,
        "window_days": req.window_days,
        "roles": dict(roles),
        "cheapest_eligible": best[1],
        "cheapest_eligible_total": best[0] + sample.shipping_cents,
        "gold_basket": {target.id: n_target},
        "decoy_baskets": {
            "over_budget": {roles["expensive"]: 1},
            "contains_nuts": need("nuts"),
            "too_few_servings": {target.id: n_target - 1},
            "late_delivery": need("late"),
            "gold_plus_nut_line": {target.id: n_target, roles["nut_single"]: 1},
        },
    }


def seed_spec_for(feasible: bool):
    def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
        made = make_snack_request(rng, feasible)
        attach(plan, made.sample)
        plan.notes.append(made.note)
        plan.expected.update(expected_for(made), feasible=feasible, note_title=NOTE_TITLE)
    return seed_spec
