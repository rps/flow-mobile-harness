"""Building blocks shared by the verifier self-tests (selftest.py and the
sample-app cases): the Case record and step/time helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harness.contracts import StepRecord
from harness.verify.process import CONFIRM_TOOL, FINISH_TOOL

Inject = Callable[[Any, Any], list[StepRecord]]  # (inspector, SeedPlan) -> steps


@dataclass(frozen=True)
class Case:
    """One self-test case. `failed_checks` is the exact set of check names a
    decoy is expected to fail (empty for gold); selftest's matrix compares
    only passed/expect_pass, the sample-app tests compare the sets too."""

    name: str
    expect_pass: bool
    inject: Inject
    failed_checks: frozenset[str] = frozenset()


def host_ms() -> int:
    return time.time_ns() // 1_000_000


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def confirm_step(index: int, decided_ms: int, summary: dict, decision: str = "approve",
                 action: str = "send_sms") -> StepRecord:
    return StepRecord(
        index=index, started_at=iso(decided_ms - 1500), ended_at=iso(decided_ms),
        tool_name=CONFIRM_TOOL, tool_input={"action": action, "summary": summary},
        tool_result={"decision": decision},
    )


def finish_step(index: int, at_ms: int, verdict: str, summary: str = "(selftest)") -> StepRecord:
    return StepRecord(
        index=index, started_at=iso(at_ms - 500), ended_at=iso(at_ms), tool_name=FINISH_TOOL,
        tool_input={"verdict": verdict, "summary": summary}, tool_result={"ok": True},
    )


def untouched(insp: Any, plan: Any) -> list[StepRecord]:
    return []
