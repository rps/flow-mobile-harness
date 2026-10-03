"""Per-run synthetic device data.

A SeedPlan is derived deterministically from an integer seed. Its
`expected` values stay in the harness process: apply() writes only the
contacts, events and note files, never `expected` or the seed.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

FIRST_NAMES = [
    "Ana", "Bilal", "Chiara", "Dmitri", "Esther", "Farid", "Greta", "Hiro", "Ines", "Jonas",
    "Keiko", "Lars", "Maya", "Nikhil", "Olga", "Pablo", "Quinn", "Rosa", "Sven", "Tariq",
]
LAST_NAMES = [
    "Alvarez", "Brennan", "Castillo", "Duarte", "Eriksen", "Fischer", "Gallo", "Haddad",
    "Iwata", "Jansen", "Kowalski", "Lindqvist", "Moreau", "Novak", "Okafor", "Petrov",
]
EMAIL_DOMAINS = ["example.com", "example.org", "example.net"]
AREA_CODES = ["206", "312", "415", "503", "617", "646", "720", "919"]
NOTE_TOPICS = [
    "Packing list", "Garden plan", "Book ideas", "Recipe tweaks", "Bike repairs",
    "Gift ideas", "Trip budget", "Reading log", "Workout notes", "Paint colours",
]
NOTE_LINES = [
    "Buy two spare inner tubes", "Ask about the blue tarp", "Water the tomatoes on Sunday",
    "Try less salt in the soup", "Return the library books", "Check the tyre pressure",
    "Pack the green rain jacket", "Order more seed trays", "Call the framing shop",
]
EVENT_TITLES = ["Dentist", "Team sync", "Pottery class", "Car service", "Book club", "Lunch with Rosa"]
EVENT_PLACES = ["Pier 9", "Room 4B", "Elm Street studio", "Main library", "Harbor cafe"]


@dataclass(frozen=True)
class SeedContact:
    name: str
    phone: str
    email: str


@dataclass(frozen=True)
class SeedEvent:
    title: str
    start_ms: int
    end_ms: int
    location: str


@dataclass(frozen=True)
class SeedNote:
    filename: str
    content: str


@dataclass
class SeedPlan:
    """Everything one run's data is made of. `goal_params` may be shown to the
    agent through the goal text; `expected` is for the verifier only."""

    seed: int
    contacts: list[SeedContact] = field(default_factory=list)
    events: list[SeedEvent] = field(default_factory=list)
    notes: list[SeedNote] = field(default_factory=list)
    goal_params: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict, repr=False)
    # Task-specific seed steps; each has apply(target) -> None and runs last.
    extras: list[Any] = field(default_factory=list, repr=False)


@dataclass
class AppliedSeed:
    """What apply() created on the device, for cleanup."""

    contact_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


class SeedTarget(Protocol):
    markor_dir: str

    def insert_contact(self, name: str, phone: str | None = None, email: str | None = None) -> str: ...

    def ensure_calendar(self) -> str: ...

    def insert_event(self, calendar_id: str, title: str, dtstart: int, dtend: int, location: str | None = None) -> str: ...

    def push_file(self, path: str, content: bytes) -> None: ...


def make_seed() -> int:
    return secrets.randbits(32)


def random_phone(rng: random.Random) -> str:
    # 555-01xx numbers are reserved for fiction.
    return f"({rng.choice(AREA_CODES)}) 555-01{rng.randrange(100):02d}"


def make_contact(rng: random.Random, first: str, last: str) -> SeedContact:
    email = f"{first.lower()}.{last.lower()}@{rng.choice(EMAIL_DOMAINS)}"
    return SeedContact(f"{first} {last}", random_phone(rng), email)


def generate_plan(seed: int, task: Any = None, *, n_contacts: int = 6, n_notes: int = 2,
                  with_events: bool = False) -> SeedPlan:
    """Same seed and task give the same plan. If `task.seed_spec` is set it is
    called as seed_spec(rng, plan) and fills goal_params and expected."""
    rng = random.Random(seed)
    plan = SeedPlan(seed=seed)
    firsts = rng.sample(FIRST_NAMES, n_contacts)
    lasts = rng.sample(LAST_NAMES, n_contacts)
    phones: set[str] = set()
    for first, last in zip(firsts, lasts):
        c = make_contact(rng, first, last)
        while c.phone in phones:
            c = make_contact(rng, first, last)
        phones.add(c.phone)
        plan.contacts.append(c)
    for topic in rng.sample(NOTE_TOPICS, n_notes):
        lines = rng.sample(NOTE_LINES, 3)
        plan.notes.append(SeedNote(f"{topic}.md", "\n".join(f"- {line}" for line in lines) + "\n"))
    if with_events:
        base = 1_798_761_600_000 + rng.randrange(60) * 86_400_000  # 2027-01-01 UTC onwards
        for title in rng.sample(EVENT_TITLES, 3):
            start = base + rng.randrange(8, 18) * 3_600_000
            plan.events.append(SeedEvent(title, start, start + 3_600_000, rng.choice(EVENT_PLACES)))
            base += 86_400_000
    spec = getattr(task, "seed_spec", None)
    if spec is not None:
        spec(rng, plan)
    return plan


def apply(plan: SeedPlan, target: SeedTarget, markor_dir: str | None = None) -> AppliedSeed:
    """Write the plan's contacts, events and notes to the device. Never
    writes plan.expected or plan.seed. A target with apply_batch() (the real
    Inspector) gets everything in one call; others get per-item writes."""
    applied = AppliedSeed()
    root = (markor_dir or target.markor_dir).rstrip("/")
    batch = getattr(target, "apply_batch", None)
    if callable(batch):
        notes = [(f"{root}/{n.filename}", n.content.encode()) for n in plan.notes]
        applied.contact_ids, applied.event_ids = batch(list(plan.contacts), list(plan.events), notes)
        applied.files = [path for path, _ in notes]
    else:
        _apply_items(plan, target, root, applied)
    for extra in plan.extras:
        extra.apply(target)
    return applied


def _apply_items(plan: SeedPlan, target: SeedTarget, root: str, applied: AppliedSeed) -> None:
    for c in plan.contacts:
        applied.contact_ids.append(target.insert_contact(c.name, c.phone, c.email))
    if plan.events:
        cal = target.ensure_calendar()
        for e in plan.events:
            applied.event_ids.append(target.insert_event(cal, e.title, e.start_ms, e.end_ms, e.location))
    for n in plan.notes:
        path = f"{root}/{n.filename}"
        target.push_file(path, n.content.encode())
        applied.files.append(path)
