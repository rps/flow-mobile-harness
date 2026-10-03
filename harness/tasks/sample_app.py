"""The sample-app tasks, in registry order. registry.py adds `*TASKS`."""

from __future__ import annotations

from harness.contracts import TaskSpec
from harness.tasks import (
    sample_c_variant_b,
    sample_d_orders_to_note_to_message,
    sample_drift_provider_vs_ui,
    sample_e_provider,
    sample_f2_place_order,
    sample_h_cancel_order,
    sample_note_to_order,
)

TASKS: list[TaskSpec] = [
    sample_d_orders_to_note_to_message.TASK,
    sample_f2_place_order.TASK,
    sample_c_variant_b.TASK,
    sample_e_provider.TASK_FULL,
    sample_e_provider.TASK_FALLBACK,
    sample_drift_provider_vs_ui.TASK,
    sample_h_cancel_order.TASK,
    sample_note_to_order.TASK,
    sample_note_to_order.TASK_INFEASIBLE,
]
