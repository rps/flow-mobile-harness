"""Flow b: cross-app transfer, a contact's details into a Markor note.

Oracle: Markor's plain files (tier 1); the source contact is seeded.
Normalisation: note matched by title as in task a. Phone passes if any
phone-like token in the body has the same last 10 digits as the seeded
number (so "+1 415-555-0123" matches "(415) 555-0123"). Email passes as a
casefolded substring. A decoy contact with the same first name is seeded.
Allowed side effects: one added file; nothing else.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import LAST_NAMES, SeedPlan, make_contact
from harness.verify.checks import (
    END_STATE, Check, VerifyContext, new_note_titled, norm_text, phones_in_text, phones_match,
)
from harness.verify.diff import AllowedChanges

TASK_ID = "b_contact_to_note"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    target = rng.choice(plan.contacts)
    first, last = target.name.split(" ", 1)
    used_last = {c.name.split(" ", 1)[1] for c in plan.contacts}
    used_phones = {c.phone for c in plan.contacts}
    decoy = make_contact(rng, first, rng.choice([n for n in LAST_NAMES if n not in used_last]))
    while decoy.phone in used_phones:
        decoy = make_contact(rng, first, decoy.name.split(" ", 1)[1])
    plan.contacts.insert(rng.randrange(len(plan.contacts) + 1), decoy)
    title = f"{first} {last} details"
    plan.goal_params.update(contact_name=target.name, title=title)
    plan.expected.update(name=target.name, phone=target.phone, email=target.email, title=title,
                         decoy_phone=decoy.phone, decoy_email=decoy.email)


def _note_exists(ctx: VerifyContext) -> tuple[bool, str]:
    path, _, detail = new_note_titled(ctx, ctx.expected["title"])
    return path is not None, detail


def _note_has_phone(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    found = phones_in_text(text)
    ok = any(phones_match(p, ctx.expected["phone"]) for p in found)
    return ok, "phone found" if ok else f"phone missing; numbers in note: {found}"


def _note_has_email(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    ok = norm_text(ctx.expected["email"]) in norm_text(text)
    return ok, "email found" if ok else "email missing"


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.B,
    goal='Look up {contact_name} in Contacts and save their phone number and email address '
         'in a new Markor note titled "{title}".',
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, _note_exists),
        Check("note_has_contact_phone", END_STATE, _note_has_phone),
        Check("note_has_contact_email", END_STATE, _note_has_email),
        AllowedChanges(files_added=1),
    ],
)
