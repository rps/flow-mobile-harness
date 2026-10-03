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


# --- Invoice Ninja API double -------------------------------------------------

API_ROOT = "https://invoicing.example"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def api_state(raw: dict) -> str:
    return "deleted" if raw.get("is_deleted") else "archived" if raw.get("archived_at") else "active"


class FakeInvoiceServer:
    """Invoice Ninja v5 double, as observed live on 2026-10-03: GET /clients and
    /invoices are paginated (meta.pagination.total_pages) and list every record
    state unless `status` narrows them; `is_deleted=true` hides deleted
    records (sic); DELETE /invoices/{id} soft-deletes (is_deleted, number
    suffixed "_Deleted"). HTTP 403 without X-Requested-With or with the default
    Python-urllib agent, 401 for a token other than `token` (None accepts any).
    `fail` maps a path to an HTTP status to return. Every request is logged as
    (method, path, query, token)."""

    def __init__(self, clients: list[dict], invoices: list[dict], fail: dict[str, int] | None = None,
                 token: str | None = None) -> None:
        self.clients, self.invoices = clients, invoices
        self.fail = dict(fail or {})
        self.token = token
        self.requests: list[tuple[str, str, dict, str | None]] = []
        self.user_agents: list[str | None] = []

    def __call__(self, req, timeout):
        import json
        import math
        import urllib.error
        from urllib.parse import parse_qs, urlsplit

        url = urlsplit(req.full_url)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        method, path = req.get_method(), url.path
        self.requests.append((method, path, q, req.get_header("X-api-token")))
        self.user_agents.append(req.get_header("User-agent"))
        ua = req.get_header("User-agent") or ""
        if not req.get_header("X-requested-with") or not ua or ua.startswith("Python-urllib"):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        if self.token is not None and req.get_header("X-api-token") != self.token:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if path in self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail[path], "error", {}, None)
        if method == "GET" and path in ("/api/v1/clients", "/api/v1/invoices"):
            rows = self.clients if path.endswith("clients") else self.invoices
            if "status" in q:
                rows = [r for r in rows if api_state(r) in q["status"].split(",")]
            if q.get("is_deleted") == "true":
                rows = [r for r in rows if not r.get("is_deleted")]
            per, page = int(q.get("per_page", 20)), int(q.get("page", 1))
            body = {"data": rows[(page - 1) * per:page * per],
                    "meta": {"pagination": {"total": len(rows), "per_page": per, "current_page": page,
                                            "total_pages": max(1, math.ceil(len(rows) / per))}}}
        elif method == "DELETE" and path.startswith("/api/v1/invoices/"):
            rec = next(r for r in self.invoices if r["id"] == path.rsplit("/", 1)[1])
            rec.update(is_deleted=True, archived_at=rec.get("archived_at") or 1, number=rec["number"] + "_Deleted")
            body = {"data": dict(rec)}
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        return _Resp(json.dumps(body).encode())


def _iso(d: str) -> str:
    """'10/02/2026' (the app's list) -> '2026-10-02' (the API); ISO passes through."""
    m, _, rest = d.partition("/")
    if not rest:
        return d
    day, _, year = rest.partition("/")
    return f"{year}-{int(m):02d}-{int(day):02d}"


def api_records(invoices: dict[str, Invoice]) -> tuple[list[dict], list[dict]]:
    """(clients, invoices) API JSON for a BizState's invoices, the inverse of
    biz_api.to_invoice. Dates become ISO; a status without an id is sent as
    its "status_<n>" suffix; items=None omits line_items (not read)."""
    from harness.verify.biz_api import STATUS_NAMES

    status_ids = {name: sid for sid, name in STATUS_NAMES.items()}
    client_ids: dict[str, str] = {}
    raws = []
    for k, inv in enumerate(invoices.values()):
        cid = client_ids.setdefault(inv.client, f"c{len(client_ids) + 1}")
        raw = {"id": f"i{k}", "number": inv.number, "client_id": cid, "amount": inv.amount,
               "status_id": status_ids.get(inv.status, inv.status.removeprefix("status_")), "date": _iso(inv.date),
               "is_deleted": inv.state == "Deleted", "archived_at": 1 if inv.state != "Active" else None}
        if inv.items is not None:
            raw["line_items"] = [{"product_key": i.name, "notes": "", "quantity": i.quantity, "cost": i.unit_cost,
                                  "line_total": i.total} for i in inv.items]
        raws.append(raw)
    clients = [{"id": cid, "name": name, "is_deleted": False} for name, cid in client_ids.items()]
    return clients, raws


class FakeApiBizInspector(FakeBizInspector):
    """FakeBizInspector whose invoices go through the real API client and
    parser: every snapshot serves `biz.invoices` as API JSON from a
    FakeInvoiceServer and reads them back with biz_api.InvoiceNinjaApi.
    invoices_complete is kept from the BizState (the real API path always
    reports True), so a case that cuts the read short tests the same
    condition on both paths."""

    def snapshot_state(self) -> BizDeviceState:
        from harness.verify.biz_api import InvoiceNinjaApi

        state = super().snapshot_state()
        server = FakeInvoiceServer(*api_records(state.biz.invoices), token="fake-token")
        state.biz.invoices = InvoiceNinjaApi(API_ROOT, "fake-token", opener=server).invoices()
        return state
