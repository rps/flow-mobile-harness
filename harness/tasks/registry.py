"""Predefined tasks. Agent-side code receives only render_goal()'s output."""

from __future__ import annotations

from harness.contracts import TaskSpec
from harness.tasks.limits import LIMITS
from harness.tasks import (
    a_markor_note, b_contact_to_note, biz_b1_hours, biz_b2_rate, biz_d_invoice, biz_h, f_send_sms, sample_app,
)

_TASKS: dict[str, TaskSpec] = {
    t.id: t for t in (a_markor_note.TASK, b_contact_to_note.TASK, f_send_sms.TASK, *sample_app.TASKS,
                      biz_b1_hours.TASK, biz_b2_rate.TASK, biz_d_invoice.TASK, biz_h.TASK)
}
for _id, _limits in LIMITS.items():
    _TASKS[_id].limits = _limits  # KeyError: limits.py names a task that is not registered


def all_tasks() -> list[TaskSpec]:
    return list(_TASKS.values())


def get(task_id: str) -> TaskSpec:
    """Raises KeyError for an unknown id."""
    return _TASKS[task_id]


def render_goal(task: TaskSpec, plan) -> str:
    """Fill the goal template from plan.goal_params (never plan.expected)."""
    return task.goal.format(**plan.goal_params)
