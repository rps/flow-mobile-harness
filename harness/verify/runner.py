"""run_verifier: score one run from device state and the trace.

task.checks holds Check objects (grouped by .group) and at most one
AllowedChanges describing the side effects the task calls for; with none,
no change at all is allowed. The agent's verdict never affects `passed`.
"""

from __future__ import annotations

from typing import Any

from harness.contracts import RunRecord, StepRecord, TaskSpec, Verdict, VerifierResult
from harness.device.inspect import DeviceState
from harness.verify.checks import END_STATE, PROCESS, SIDE_EFFECTS, Check, VerifyContext
from harness.verify.diff import AllowedChanges, diff_states, side_effect_results


def run_verifier(task: TaskSpec, plan: Any, pre_state: DeviceState, post_state: DeviceState,
                 run_record: RunRecord | None, steps: list[StepRecord]) -> VerifierResult:
    diff = diff_states(pre_state, post_state)
    ctx = VerifyContext(plan, pre_state, post_state, diff, run_record, list(steps))
    allowed = [c for c in task.checks if isinstance(c, AllowedChanges)]
    if len(allowed) > 1:
        raise ValueError(f"task {task.id} declares more than one AllowedChanges")
    checks = [c for c in task.checks if isinstance(c, Check)]
    end_state = [c(ctx) for c in checks if c.group == END_STATE]
    side_effects = side_effect_results(diff, pre_state, post_state, allowed[0] if allowed else AllowedChanges())
    side_effects += [c(ctx) for c in checks if c.group == SIDE_EFFECTS]
    process = [c(ctx) for c in checks if c.group == PROCESS]
    groups = end_state + side_effects + process
    passed = bool(end_state) and all(r.passed for r in groups)
    verdict = run_record.agent_verdict if run_record else None
    return VerifierResult(
        passed=passed,
        oracle_tier=task.oracle_tier,
        end_state=end_state,
        side_effects=side_effects,
        process=process,
        self_report_agrees=None if verdict is None else (verdict == Verdict.DONE) == passed,
    )
