"""Per-task run limits: steps, agent wall-clock seconds and model spend.

Sized from the runs of 2026-10-02/03 (the owner's benchmark notes and the
runs/ directory): the step and spend caps are roughly twice what a passing
run used, so a run stuck in an action loop stops well before the global
budget feels it. The wall-clock caps are sized for the cloud VM, whose one
comparable run took 182 s against 102-142 s on the Mac (x86_64 guest on 4
shared vCPU), and for the business apps, which wait on vendor servers.

Observed passing runs (steps, cost, Mac wall time) are in the comments.
"""

from __future__ import annotations

from harness.contracts import RunLimits

LIMITS: dict[str, RunLimits] = {
    "a_markor_note": RunLimits(30, 480, 0.80),  # 16, $0.37, 102-142 s (VM 182 s)
    "b_contact_to_note": RunLimits(30, 540, 0.90),  # 17, $0.41, 126-191 s
    "f_send_sms": RunLimits(20, 420, 0.60),  # 9-10, $0.21-0.23, 90-135 s
    "f2_place_order": RunLimits(20, 420, 0.80),  # 6-12, $0.18-0.38, 51-135 s
    "c_variant_b": RunLimits(20, 420, 0.70),  # 6-10, $0.18-0.33, 51-126 s
    "h_cancel_order": RunLimits(20, 420, 0.60),  # 6-9, $0.15-0.27, 53-110 s
    "e_provider_full": RunLimits(30, 600, 0.90),  # 17, $0.40-0.43, 108-140 s
    "e_provider_fallback": RunLimits(30, 600, 0.90),  # 17-18, $0.40-0.44, 106-212 s
    "drift_provider_vs_ui": RunLimits(35, 660, 1.20),  # 19-22, $0.48-0.58, 113-215 s
    "d_orders_to_note_to_message": RunLimits(40, 720, 1.40),  # 25, $0.65-0.67, 176-217 s
    "b2_note_to_order": RunLimits(60, 1200, 2.50),  # 35, $1.13, 385 s
    "h2_note_to_order_infeasible": RunLimits(70, 1800, 3.00),  # 51, $1.90-1.91, 560-606 s
    "biz_b1_hours": RunLimits(50, 1200, 1.80),  # 19-21, $0.56-0.64, 289-339 s (VM 31 steps)
    "biz_b2_rate": RunLimits(50, 1200, 1.80),  # 20-23, $0.51-0.64, 263-375 s (VM 319-375 s)
    "biz_d_invoice": RunLimits(50, 1200, 1.80),  # 14-20, $0.48-0.67, 211-311 s (VM 28 steps)
    "biz_h": RunLimits(25, 720, 0.90),  # 9-14, $0.22-0.40, 151-212 s
}
