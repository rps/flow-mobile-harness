"""Check type, verification context and normalisation helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from harness.contracts import CheckResult, RunRecord, StepRecord
from harness.device.inspect import SMS_OUTGOING, DeviceState, Sms
from harness.verify.diff import StateDiff

END_STATE, SIDE_EFFECTS, PROCESS = "end_state", "side_effects", "process"

NOTE_SUFFIXES = (".md", ".txt")


@dataclass
class VerifyContext:
    plan: Any  # SeedPlan
    pre: DeviceState
    post: DeviceState
    diff: StateDiff
    run_record: RunRecord | None = None
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def expected(self) -> dict[str, Any]:
        return self.plan.expected


@dataclass(frozen=True)
class Check:
    """A named predicate in one result group. Calling it never raises:
    an exception inside the predicate becomes a failed CheckResult."""

    name: str
    group: str
    fn: Callable[[VerifyContext], tuple[bool, str]]

    def __call__(self, ctx: VerifyContext) -> CheckResult:
        try:
            ok, detail = self.fn(ctx)
        except Exception as e:  # a broken check must fail, not pass silently
            return CheckResult(self.name, False, f"check error: {type(e).__name__}: {e}")
        return CheckResult(self.name, bool(ok), detail)


# --- Normalisation -----------------------------------------------------------


def norm_text(s: str | None) -> str:
    """CRLF to LF, collapse whitespace runs, strip ends, casefold."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s.replace("\r\n", "\n")).strip().casefold()


def norm_phone(s: str | None) -> str:
    """Digits only, last 10 digits (drops a +1 or 1 country prefix)."""
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:]


def phones_match(a: str | None, b: str | None) -> bool:
    na, nb = norm_phone(a), norm_phone(b)
    return len(na) >= 7 and na == nb


# Spaces and tabs only, so a number never runs into digits on the next line.
_PHONE_TOKEN = re.compile(r"\+?\(?\d[\d \t().-]{5,}\d")


def phones_in_text(text: str) -> list[str]:
    return [norm_phone(m.group()) for m in _PHONE_TOKEN.finditer(text)]


def note_title(path: str) -> str | None:
    """Normalised title of a note file, or None if not a note."""
    p = PurePosixPath(path)
    if p.suffix.lower() not in NOTE_SUFFIXES:
        return None
    return norm_text(p.stem)


# --- Shared state helpers ----------------------------------------------------


def new_notes(ctx: VerifyContext) -> dict[str, str]:
    """Files added during the run, path -> text."""
    return {p: ctx.post.files[p].text for p in ctx.diff.files_added}


def new_note_titled(ctx: VerifyContext, title: str) -> tuple[str | None, str, str]:
    """Return (path, text, detail) of the single added note whose stem matches
    `title`. path is None when there is no match or more than one."""
    want = norm_text(title)
    added = new_notes(ctx)
    hits = [p for p in added if note_title(p) == want]
    if len(hits) == 1:
        return hits[0], added[hits[0]], hits[0]
    if not hits:
        return None, "", f"no new note titled {title!r}; added: {sorted(added) or 'none'}"
    return None, "", f"{len(hits)} new notes titled {title!r}: {hits}"


def new_outgoing_sms(ctx: VerifyContext) -> list[Sms]:
    return [ctx.post.sms[i] for i in ctx.diff.sms_added if ctx.post.sms[i].type in SMS_OUTGOING]
