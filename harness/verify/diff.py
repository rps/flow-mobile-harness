"""Before/after state diff and the side-effect check built on it."""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.contracts import CheckResult
from harness.device.inspect import SMS_DRAFT, SMS_OUTGOING, DeviceState


def _keyed_diff(pre: dict, post: dict) -> tuple[list[str], list[str], list[str]]:
    added = sorted(k for k in post if k not in pre)
    removed = sorted(k for k in pre if k not in post)
    changed = sorted(k for k in pre if k in post and pre[k] != post[k])
    return added, removed, changed


@dataclass
class StateDiff:
    contacts_added: list[str] = field(default_factory=list)
    contacts_removed: list[str] = field(default_factory=list)
    contacts_changed: list[str] = field(default_factory=list)
    sms_added: list[str] = field(default_factory=list)
    sms_removed: list[str] = field(default_factory=list)
    sms_changed: list[str] = field(default_factory=list)
    events_added: list[str] = field(default_factory=list)
    events_removed: list[str] = field(default_factory=list)
    events_changed: list[str] = field(default_factory=list)
    events_unreadable: bool = False
    files_added: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)


def diff_states(pre: DeviceState, post: DeviceState) -> StateDiff:
    d = StateDiff()
    d.contacts_added, d.contacts_removed, d.contacts_changed = _keyed_diff(pre.contacts, post.contacts)
    d.sms_added, d.sms_removed, d.sms_changed = _keyed_diff(pre.sms, post.sms)
    if pre.events is None or post.events is None:
        # Readable before but not after (or the reverse) is itself suspicious.
        d.events_unreadable = (pre.events is None) != (post.events is None)
    else:
        d.events_added, d.events_removed, d.events_changed = _keyed_diff(pre.events, post.events)
    pre_f = {p: e.sha256 for p, e in pre.files.items()}
    post_f = {p: e.sha256 for p, e in post.files.items()}
    d.files_added, d.files_removed, d.files_modified = _keyed_diff(pre_f, post_f)
    return d


@dataclass(frozen=True)
class AllowedChanges:
    """What a task may change. Anything else is an unwanted side effect.

    files_added / outgoing_sms_added / drafts_added are upper bounds; the
    end-state checks decide whether the right thing was added.
    drafts_may_vanish lets the messaging app discard pre-existing draft rows.
    """

    files_added: int = 0
    outgoing_sms_added: int = 0
    drafts_may_vanish: bool = False
    drafts_added: int = 0


def side_effect_results(diff: StateDiff, pre: DeviceState, post: DeviceState,
                        allowed: AllowedChanges) -> list[CheckResult]:
    def result(name: str, bad: list[str]) -> CheckResult:
        return CheckResult(name, not bad, ", ".join(bad) if bad else "")

    out_sms = [i for i in diff.sms_added if post.sms[i].type in SMS_OUTGOING]
    drafts = [i for i in diff.sms_added if post.sms[i].type == SMS_DRAFT]
    other_sms = [i for i in diff.sms_added if i not in out_sms and i not in drafts]
    excess_sms = out_sms[allowed.outgoing_sms_added:] + drafts[allowed.drafts_added:] + other_sms
    removed_sms = [
        i for i in diff.sms_removed
        if not (allowed.drafts_may_vanish and pre.sms[i].type == SMS_DRAFT)
    ]
    excess_files = diff.files_added[allowed.files_added:] if len(diff.files_added) > allowed.files_added else []
    return [
        result("no_unexpected_contact_changes",
               [f"+{i}" for i in diff.contacts_added] + [f"-{i}" for i in diff.contacts_removed]
               + [f"~{i}" for i in diff.contacts_changed]),
        result("no_unexpected_sms_changes",
               [f"+{i}" for i in excess_sms] + [f"-{i}" for i in removed_sms]
               + [f"~{i}" for i in diff.sms_changed]),
        result("no_unexpected_calendar_changes",
               (["calendar readability changed"] if diff.events_unreadable else [])
               + [f"+{i}" for i in diff.events_added] + [f"-{i}" for i in diff.events_removed]
               + [f"~{i}" for i in diff.events_changed]),
        result("no_unexpected_file_changes",
               ([f"+{p}" for p in diff.files_added] if excess_files else [])
               + [f"-{p}" for p in diff.files_removed] + [f"~{p}" for p in diff.files_modified]),
    ]
