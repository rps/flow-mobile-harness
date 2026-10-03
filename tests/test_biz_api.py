"""Invoice Ninja API oracle: URL handling, parsing, pagination, errors and the invoice cleanup."""

import urllib.error

import pytest

from harness.contracts import OracleTier
from harness.verify import biz_api
from harness.verify.biz_api import InvoiceNinjaApi, InvoiceNinjaError
from harness.verify.biz_readback import Invoice, LineItem
from tests.biz_fakes import API_ROOT, FakeInvoiceServer

TOKEN = "tok-SECRET-123"


def _client(cid, name, deleted=False):
    return {"id": cid, "name": name, "is_deleted": deleted, "archived_at": None}


def _inv(iid, number, cid, amount=403.75, status_id="1", deleted=False, archived=False, items=None):
    return {"id": iid, "number": number, "client_id": cid, "amount": amount, "status_id": status_id,
            "date": "2026-10-02", "is_deleted": deleted, "archived_at": 1700000000 if (archived or deleted) else None,
            "line_items": items if items is not None else [
                {"product_key": "Consulting hours", "notes": "", "quantity": 4.25, "cost": 95, "line_total": 403.75}]}


def _server(**kw):
    """The account as observed live on 2026-10-03 before cleanup, plus one
    invoice of the other client so the cleanup has something to leave alone."""
    clients = [_client("nw", "Northwind Traders"), _client("zz", "ZZ Probe Test Client")]
    invoices = [_inv("a", "0001_Deleted", "nw", deleted=True), _inv("b", "0002", "nw"),
                _inv("c", "0003", "zz", amount=120.0, status_id="2", items=[
                    {"product_key": "", "notes": "Setup", "quantity": "1", "cost": "120", "line_total": "120.00"}])]
    return FakeInvoiceServer(clients, invoices, token=TOKEN, **kw)


def _api(server):
    return InvoiceNinjaApi(API_ROOT, TOKEN, opener=server)


@pytest.mark.parametrize("endpoint", [
    "https://invoicing.co", "https://invoicing.co/", "invoicing.co", "  https://invoicing.co/api/v1/ ",
    "https://invoicing.co/api/v1",
])
def test_base_url_normalises_a_bare_host_or_a_full_api_root(endpoint):
    assert biz_api.base_url(endpoint) == "https://invoicing.co/api/v1"


@pytest.mark.parametrize("endpoint", [
    "http://invoicing.co", "https://invoicing.co/app", "https://invoicing.co/api/v1?x=1", "https://", "ftp://invoicing.co",
])
def test_base_url_refuses_plain_http_odd_paths_and_queries(endpoint):
    with pytest.raises(InvoiceNinjaError, match="INVOICE_NINJA_ENDPOINT"):
        biz_api.base_url(endpoint)


def test_from_env_and_tier_need_both_variables():
    full = {biz_api.ENV_KEY: TOKEN, biz_api.ENV_ENDPOINT: "https://invoicing.co"}
    api = biz_api.from_env(full)
    assert api is not None and api.base == "https://invoicing.co/api/v1"
    assert TOKEN not in repr(api)
    assert biz_api.invoice_oracle_tier(full) == OracleTier.APP_EXPORT_API
    for env in ({}, {biz_api.ENV_KEY: TOKEN}, {biz_api.ENV_ENDPOINT: "https://invoicing.co"},
                {biz_api.ENV_KEY: "  ", biz_api.ENV_ENDPOINT: "https://invoicing.co"}):
        assert biz_api.from_env(env) is None
        assert biz_api.invoice_oracle_tier(env) == OracleTier.SCRIPTED_READBACK


def test_invoices_parse_every_state_status_client_and_line_item():
    server = _server()
    out = _api(server).invoices()
    assert out == {
        "0001_Deleted": Invoice("0001_Deleted", "Northwind Traders", 403.75, "Draft", "2026-10-02",
                                (LineItem("Consulting hours", 403.75, 4.25, 95.0),), "Deleted"),
        "0002": Invoice("0002", "Northwind Traders", 403.75, "Draft", "2026-10-02",
                        (LineItem("Consulting hours", 403.75, 4.25, 95.0),), "Active"),
        "0003": Invoice("0003", "ZZ Probe Test Client", 120.0, "Sent", "2026-10-02",
                        (LineItem("Setup", 120.0, 1.0, 120.0),), "Active"),  # notes used when product_key is empty
    }


def test_every_request_asks_for_all_states_and_carries_the_headers():
    server = _server()
    _api(server).invoices()
    assert {(m, p) for m, p, _, _ in server.requests} == {("GET", "/api/v1/clients"), ("GET", "/api/v1/invoices")}
    assert all(q["status"] == "active,archived,deleted" for _, _, q, _ in server.requests)
    assert all("is_deleted" not in q for _, _, q, _ in server.requests)  # is_deleted=true would hide deleted rows
    assert all(tok == TOKEN for *_, tok in server.requests)
    # The hosted service answers the default Python-urllib agent with 403.
    assert all(ua and not ua.startswith("Python-urllib") for ua in server.user_agents)


def test_the_fake_server_enforces_the_live_quirks():
    """Guards the double itself: without these the header test above could pass vacuously."""
    import urllib.request

    server = _server()
    bare = urllib.request.Request(API_ROOT + "/api/v1/clients", headers={"X-API-TOKEN": TOKEN})
    with pytest.raises(urllib.error.HTTPError) as exc:
        server(bare, 1)
    assert exc.value.code == 403
    hidden = urllib.request.Request(API_ROOT + "/api/v1/invoices?is_deleted=true", headers={
        "X-API-TOKEN": TOKEN, "X-Requested-With": "XMLHttpRequest", "User-Agent": "x"})
    import json
    numbers = [r["number"] for r in json.loads(server(hidden, 1).read())["data"]]
    assert "0001_Deleted" not in numbers and "0002" in numbers


def test_wrong_token_is_reported_as_http_401_without_the_token():
    server = _server()
    with pytest.raises(InvoiceNinjaError, match="HTTP 401") as exc:
        InvoiceNinjaApi(API_ROOT, "wrong-token", opener=server).invoices()
    assert "wrong-token" not in str(exc.value)


def test_missing_line_items_mean_not_read_and_an_empty_list_means_none():
    names = {"nw": "Northwind Traders"}
    raw = _inv("a", "0002", "nw")
    del raw["line_items"]
    assert biz_api.to_invoice(raw, names).items is None
    assert biz_api.to_invoice(_inv("a", "0002", "nw", items=[]), names).items == ()


def test_archived_and_unknown_status_are_reported_not_guessed():
    server = FakeInvoiceServer([_client("nw", "Northwind Traders")],
                               [_inv("x", "0009", "nw", status_id=9, archived=True), _inv("y", "0010", "gone")],
                               token=TOKEN)
    out = _api(server).invoices()
    assert (out["0009"].state, out["0009"].status) == ("Archived", "status_9")
    assert out["0010"].client == ""  # client not listed: no name rather than a wrong one


def test_pagination_reads_every_page(monkeypatch):
    monkeypatch.setattr(biz_api, "PER_PAGE", 2)
    invoices = [_inv(f"i{k}", f"{k:04d}", "nw") for k in range(1, 6)]
    server = FakeInvoiceServer([_client("nw", "Northwind Traders")], invoices)
    assert sorted(_api(server).invoices()) == ["0001", "0002", "0003", "0004", "0005"]
    assert [q["page"] for _, p, q, _ in server.requests if p.endswith("/invoices")] == ["1", "2", "3"]


def test_pagination_has_a_cap(monkeypatch):
    monkeypatch.setattr(biz_api, "PER_PAGE", 1)
    monkeypatch.setattr(biz_api, "MAX_PAGES", 2)
    server = FakeInvoiceServer([_client("nw", "Northwind Traders")], [_inv(f"i{k}", f"{k:04d}", "nw") for k in range(3)])
    with pytest.raises(InvoiceNinjaError, match="more than 2 pages"):
        _api(server).raw_invoices()


def test_duplicate_numbers_raise_instead_of_one_silently_winning():
    server = FakeInvoiceServer([_client("nw", "N")], [_inv("a", "0002", "nw"), _inv("b", "0002", "nw")])
    with pytest.raises(InvoiceNinjaError, match="listed twice"):
        _api(server).invoices()


def test_http_errors_name_the_call_and_never_the_token():
    server = _server(fail={"/api/v1/invoices": 403})
    with pytest.raises(InvoiceNinjaError) as exc:
        _api(server).invoices()
    assert "GET /invoices: HTTP 403" in str(exc.value) and TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_network_errors_bad_json_and_missing_data_raise():
    def down(req, timeout):
        raise urllib.error.URLError("nodename nor servname provided")

    with pytest.raises(InvoiceNinjaError, match="URLError"):
        InvoiceNinjaApi(API_ROOT, TOKEN, opener=down).clients()

    from tests.biz_fakes import _Resp

    with pytest.raises(InvoiceNinjaError, match="not JSON"):
        InvoiceNinjaApi(API_ROOT, TOKEN, opener=lambda r, t: _Resp(b"<html>cloudflare</html>")).clients()
    with pytest.raises(InvoiceNinjaError, match="no 'data' list"):
        InvoiceNinjaApi(API_ROOT, TOKEN, opener=lambda r, t: _Resp(b'{"message": "Invalid token"}')).clients()


def test_non_numeric_amount_raises():
    server = FakeInvoiceServer([_client("nw", "N")], [_inv("a", "0002", "nw", amount="n/a")])
    with pytest.raises(InvoiceNinjaError, match="amount is not a number"):
        _api(server).invoices()


def test_empty_token_is_refused():
    with pytest.raises(InvoiceNinjaError, match="empty"):
        InvoiceNinjaApi(API_ROOT, "")


# --- cleanup ----------------------------------------------------------------------


def test_cleanup_deletes_only_the_clients_live_invoices():
    server = _server()
    server.invoices.append(_inv("d", "0004", "nw", archived=True))
    out = biz_api.cleanup_client_invoices(_api(server), "Northwind Traders")
    deletes = [p for m, p, _, _ in server.requests if m == "DELETE"]
    assert deletes == ["/api/v1/invoices/b", "/api/v1/invoices/d"]  # active and archived; not the deleted one
    assert out == {"client": "Northwind Traders", "remaining_not_deleted": 0, "removed": [
        {"number": "0002", "amount": 403.75, "status": "Draft", "state_before": "Active", "number_after": "0002_Deleted"},
        {"number": "0004", "amount": 403.75, "status": "Draft", "state_before": "Archived", "number_after": "0004_Deleted"},
    ]}
    other = next(r for r in server.invoices if r["id"] == "c")
    assert other["number"] == "0003" and not other["is_deleted"]
    after = _api(server).invoices()
    assert [n for n, i in after.items() if i.client == "Northwind Traders" and i.state != "Deleted"] == []


def test_cleanup_with_nothing_to_remove_deletes_nothing():
    server = _server()
    server.invoices[:] = [r for r in server.invoices if r["id"] != "b"]
    out = biz_api.cleanup_client_invoices(_api(server), "Northwind Traders")
    assert out["removed"] == [] and not [r for r in server.requests if r[0] == "DELETE"]


@pytest.mark.parametrize("clients", [
    [_client("zz", "ZZ Probe Test Client")],
    [_client("nw", "Northwind Traders"), _client("nw2", "Northwind Traders")],
    [_client("nw", "northwind traders")],
])
def test_cleanup_refuses_unless_exactly_one_client_matches_exactly(clients):
    server = FakeInvoiceServer(clients, [_inv("b", "0002", "nw"), _inv("b2", "0005", "nw2")])
    with pytest.raises(InvoiceNinjaError, match="exactly one client"):
        biz_api.cleanup_client_invoices(_api(server), "Northwind Traders")
    assert not [r for r in server.requests if r[0] == "DELETE"]


def test_cleanup_ignores_a_deleted_namesake_client():
    server = _server()
    server.clients.append(_client("old", "Northwind Traders", deleted=True))
    server.invoices.append(_inv("o", "0008", "old"))
    biz_api.cleanup_client_invoices(_api(server), "Northwind Traders")
    assert [p for m, p, _, _ in server.requests if m == "DELETE"] == ["/api/v1/invoices/b"]


def test_cleanup_raises_when_a_delete_did_not_take():
    server = _server()
    real = server.__call__

    def no_op_delete(req, timeout):
        if req.get_method() == "DELETE":
            server.requests.append(("DELETE", req.full_url, {}, None))
            from tests.biz_fakes import _Resp
            return _Resp(b'{"data": {}}')
        return real(req, timeout)

    with pytest.raises(InvoiceNinjaError, match="still not deleted"):
        biz_api.cleanup_client_invoices(InvoiceNinjaApi(API_ROOT, TOKEN, opener=no_op_delete), "Northwind Traders")


def test_cleanup_stops_at_the_first_failed_delete():
    server = _server(fail={"/api/v1/invoices/b": 500})
    with pytest.raises(InvoiceNinjaError, match="DELETE /invoices/b: HTTP 500"):
        biz_api.cleanup_client_invoices(_api(server), "Northwind Traders")
