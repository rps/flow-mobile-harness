"""Flow a: create a Markor note with given content.

Oracle: Markor's plain files (tier 1).
Normalisation: the note is any added .md/.txt file whose stem equals the
title after norm_text (whitespace collapsed, casefolded). The body passes if
norm_text(content) is a substring of norm_text(body), so a title or header
line added by the app is tolerated.
Allowed side effects: one added file; nothing else.
"""

from __future__ import annotations

import random

from harness.contracts import FlowType, OracleTier, TaskSpec
from harness.seed.generator import NOTE_LINES, NOTE_TOPICS, SeedPlan
from harness.verify.checks import END_STATE, Check, VerifyContext, new_note_titled, norm_text
from harness.verify.diff import AllowedChanges

TASK_ID = "a_markor_note"


def seed_spec(rng: random.Random, plan: SeedPlan) -> None:
    used = {n.filename.rsplit(".", 1)[0] for n in plan.notes}
    topic = rng.choice([t for t in NOTE_TOPICS if t not in used])
    title = f"{topic} {rng.randrange(10, 100)}"
    content = f"{rng.choice(NOTE_LINES)} before {rng.choice(['Monday', 'Friday', 'noon', 'the 14th'])}"
    plan.goal_params.update(title=title, content=content)
    plan.expected.update(title=title, content=content)


def _note_exists(ctx: VerifyContext) -> tuple[bool, str]:
    path, _, detail = new_note_titled(ctx, ctx.expected["title"])
    return path is not None, detail


def _note_content(ctx: VerifyContext) -> tuple[bool, str]:
    path, text, detail = new_note_titled(ctx, ctx.expected["title"])
    if path is None:
        return False, detail
    ok = norm_text(ctx.expected["content"]) in norm_text(text)
    return ok, "content found" if ok else f"content missing; note has {len(text)} chars"


TASK = TaskSpec(
    id=TASK_ID,
    flow_type=FlowType.A,
    goal='In Markor, create a new note titled "{title}" with this text: {content}',
    oracle_tier=OracleTier.OWN_STORAGE,
    seed_spec=seed_spec,
    checks=[
        Check("note_created_with_title", END_STATE, _note_exists),
        Check("note_has_content", END_STATE, _note_content),
        AllowedChanges(files_added=1),
    ],
)
