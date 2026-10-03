"""Flow c: f2_place_order under UI variant B (different labels and layout,
same function). Goal, oracle and checks are f2's; only the seed differs.
"""

from __future__ import annotations

from harness.contracts import FlowType
from harness.tasks.sample_f2_place_order import order_task

TASK_ID = "c_variant_b"

TASK = order_task(TASK_ID, FlowType.C, "B")
