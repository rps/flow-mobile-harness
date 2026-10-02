"""Predefined tasks. Agent-side code receives only render_goal()'s output."""

from __future__ import annotations

from harness.contracts import TaskSpec
from harness.tasks import a_markor_note, b_contact_to_note, f_send_sms

_TASKS: dict[str, TaskSpec] = {
    t.id: t for t in (a_markor_note.TASK, b_contact_to_note.TASK, f_send_sms.TASK)
}


def all_tasks() -> list[TaskSpec]:
    return list(_TASKS.values())


def get(task_id: str) -> TaskSpec:
    """Raises KeyError for an unknown id."""
    return _TASKS[task_id]


def render_goal(task: TaskSpec, plan) -> str:
    """Fill the goal template from plan.goal_params (never plan.expected)."""
    return task.goal.format(**plan.goal_params)
