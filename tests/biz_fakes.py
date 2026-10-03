"""In-memory inspector carrying a BizState, for the business-task self-tests."""

from __future__ import annotations

import copy

from harness.verify.biz_readback import CLIENT_ORG, DECOY_ORG, BizDeviceState, BizState, Invoice, LineItem
from tests.fakes import FakeInspector


def snapshot_biz_state() -> BizState:
    """What snapshot `business` showed on 2026-10-02 (see apps-probe/PROBE.md)."""
    return BizState(
        project_hours={"Ideation": 5.0, CLIENT_ORG: 4.25, "Android Flow": 2.0},
        org_descriptions={CLIENT_ORG: "Hourly rate: 95 USD per hour", DECOY_ORG: "Hourly rate: 80 USD per hour"},
        invoices={"0001": Invoice("0001", "ZZ Probe Test Client", 120.0, "Draft", "10/01/2026",
                                  (LineItem("Setup", 120.0, 1.0, 120.0),)),
                  "0000_Deleted": Invoice("0000_Deleted", "ZZ Probe Test Client", 5.0, "Draft", "09/30/2026", None, "Deleted")},
    )


class FakeBizInspector(FakeInspector):
    """Returns a deep copy of `biz` on every snapshot. Unlike BusinessInspector
    it does not cache sources across calls and always carries line items
    (the real pre-state has items=None); test_biz_readback covers those."""

    def __init__(self, biz: BizState | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.biz = biz if biz is not None else snapshot_biz_state()

    def snapshot_state(self) -> BizDeviceState:
        base = super().snapshot_state()
        return BizDeviceState(contacts=base.contacts, sms=base.sms, events=base.events, files=base.files,
                              device_time_offset_ms=base.device_time_offset_ms, biz=copy.deepcopy(self.biz))
