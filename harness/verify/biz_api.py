"""Invoice Ninja v5 REST oracle for the business profile (tier 2, APP_EXPORT_API).

Used instead of the tier-5 screen read-back for invoices when both
INVOICE_NINJA_API_KEY and INVOICE_NINJA_ENDPOINT are set in os.environ (never
read from .env by this module). The endpoint may be the bare host
("https://invoicing.co") or already end in /api/v1; base_url() normalises it.
Verified live on 2026-10-03 against the hosted service (owner's probe notes):

- GET {base}/invoices?status=active,archived,deleted&per_page=N&page=P returns
  {"data": [...], "meta": {"pagination": {"total_pages": ...}}}. Without the
  status filter deleted invoices are listed too; with is_deleted=true they are
  NOT (counter-intuitive), so only the status filter is used.
- A deleted invoice keeps its record with number "<n>_Deleted" and
  is_deleted true, the same number the app's list shows.
- Requests need X-API-TOKEN, X-Requested-With: XMLHttpRequest and an explicit
  User-Agent: the default "Python-urllib/x" agent gets HTTP 403.

Standard library only. Never imported by harness/agent. The token is never
logged, put in an exception message or returned.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from harness.contracts import DeviceError, OracleTier
from harness.verify.biz_readback import Invoice, LineItem

ENV_KEY = "INVOICE_NINJA_API_KEY"
ENV_ENDPOINT = "INVOICE_NINJA_ENDPOINT"
API_PATH = "/api/v1"
USER_AGENT = "labs-harness/1.0"
ALL_STATES = "active,archived,deleted"
PER_PAGE = 100
MAX_PAGES = 50
TIMEOUT_S = 30.0
# Invoice Ninja v5 invoice status_id values; Overdue/Unpaid are computed by the app, not stored.
STATUS_NAMES = {"1": "Draft", "2": "Sent", "3": "Partial", "4": "Paid", "5": "Cancelled", "6": "Reversed"}


class InvoiceNinjaError(DeviceError):
    """An API call failed or returned something unexpected."""


def base_url(endpoint: str) -> str:
    """'https://invoicing.co', 'invoicing.co/', '.../api/v1/' -> 'https://invoicing.co/api/v1'.
    Only https is accepted: the token travels in a header."""
    e = endpoint.strip()
    if "://" not in e:
        e = "https://" + e
    parts = urllib.parse.urlsplit(e)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise InvoiceNinjaError(f"{ENV_ENDPOINT} must be an https URL of the form https://<host>[{API_PATH}]")
    path = parts.path.rstrip("/")
    if path not in ("", API_PATH):
        raise InvoiceNinjaError(f"{ENV_ENDPOINT} has an unexpected path; give https://<host> or https://<host>{API_PATH}")
    return f"https://{parts.netloc}{API_PATH}"


Opener = Callable[[urllib.request.Request, float], Any]


def _urlopen(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class InvoiceNinjaApi:
    def __init__(self, endpoint: str, token: str, opener: Opener = _urlopen, timeout: float = TIMEOUT_S) -> None:
        if not token:
            raise InvoiceNinjaError(f"{ENV_KEY} is empty")
        self.base = base_url(endpoint)
        self._token = token
        self._open = opener
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"InvoiceNinjaApi({self.base!r})"

    def _request(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, headers={
            "X-API-TOKEN": self._token, "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json", "User-Agent": USER_AGENT,
        })
        try:
            with self._open(req, self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise InvoiceNinjaError(f"{method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise InvoiceNinjaError(f"{method} {path}: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise InvoiceNinjaError(f"{method} {path}: response is not JSON") from None

    def _list(self, path: str) -> list[dict]:
        """Every record of a list endpoint, all record states, all pages."""
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._request("GET", path, {"status": ALL_STATES, "per_page": PER_PAGE, "page": page})
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise InvoiceNinjaError(f"GET {path}: no 'data' list in the response")
            out += data
            pages = ((body.get("meta") or {}).get("pagination") or {}).get("total_pages", 1)
            if page >= int(pages or 1):
                return out
        raise InvoiceNinjaError(f"GET {path}: more than {MAX_PAGES} pages")

    def clients(self) -> list[dict]:
        return self._list("/clients")

    def raw_invoices(self) -> list[dict]:
        return self._list("/invoices")

    def invoices(self) -> dict[str, Invoice]:
        """number -> Invoice (line items always read), Active, Archived and Deleted."""
        names = {c.get("id"): c.get("name", "") for c in self.clients()}
        out: dict[str, Invoice] = {}
        for raw in self.raw_invoices():
            inv = to_invoice(raw, names)
            if inv.number in out:
                raise InvoiceNinjaError(f"invoice number {inv.number!r} listed twice")
            out[inv.number] = inv
        return out

    def delete_invoice(self, invoice_id: str) -> dict:
        """Soft delete (the record stays, numbered '<n>_Deleted'). Returns the
        updated record."""
        body = self._request("DELETE", f"/invoices/{urllib.parse.quote(invoice_id, safe='')}")
        return body.get("data", body) if isinstance(body, dict) else {}


def _num(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise InvoiceNinjaError(f"{what} is not a number: {value!r}") from None


def record_state(raw: Mapping[str, Any]) -> str:
    """Active / Archived / Deleted, as the app's list labels them."""
    if raw.get("is_deleted"):
        return "Deleted"
    return "Archived" if raw.get("archived_at") else "Active"


def to_invoice(raw: Mapping[str, Any], client_names: Mapping[str, str]) -> Invoice:
    number = raw.get("number")
    if not number:
        raise InvoiceNinjaError(f"invoice {raw.get('id')!r} has no number")
    status_id = str(raw.get("status_id", ""))
    lines = raw.get("line_items")
    items = None if lines is None else tuple(  # None: the response carried no line items (not read)
        LineItem(str(i.get("product_key") or i.get("notes") or ""),
                 _num(i.get("line_total"), f"invoice {number} line total"),
                 _num(i.get("quantity"), f"invoice {number} quantity"),
                 _num(i.get("cost"), f"invoice {number} unit cost"))
        for i in lines)
    return Invoice(number=str(number), client=client_names.get(raw.get("client_id"), ""),
                   amount=_num(raw.get("amount"), f"invoice {number} amount"),
                   status=STATUS_NAMES.get(status_id, f"status_{status_id}"),
                   date=str(raw.get("date") or ""), items=items, state=record_state(raw))


def from_env(environ: Mapping[str, str] | None = None, opener: Opener = _urlopen) -> InvoiceNinjaApi | None:
    """The API client when both variables are set and non-empty, else None
    (the caller falls back to the screen read-back)."""
    env = os.environ if environ is None else environ
    endpoint, token = env.get(ENV_ENDPOINT, "").strip(), env.get(ENV_KEY, "").strip()
    if not endpoint or not token:
        return None
    return InvoiceNinjaApi(endpoint, token, opener=opener)


def invoice_oracle_tier(environ: Mapping[str, str] | None = None) -> OracleTier:
    """Tier of the invoice oracle the current environment selects."""
    env = os.environ if environ is None else environ
    configured = env.get(ENV_ENDPOINT, "").strip() and env.get(ENV_KEY, "").strip()
    return OracleTier.APP_EXPORT_API if configured else OracleTier.SCRIPTED_READBACK


def cleanup_client_invoices(api: InvoiceNinjaApi, client_name: str) -> dict:
    """Delete every not-yet-deleted invoice (active or archived) of the one
    client named exactly `client_name`; no other client's invoice is touched.
    Refuses unless exactly one non-deleted client has that name. Returns what
    was removed and what remains, for run meta."""
    matches = [c for c in api.clients() if c.get("name") == client_name and not c.get("is_deleted")]
    if len(matches) != 1:
        raise InvoiceNinjaError(f"expected exactly one client named {client_name!r}, found {len(matches)}")
    client_id = matches[0].get("id")
    removed = []
    for raw in api.raw_invoices():
        if raw.get("client_id") != client_id or raw.get("is_deleted"):
            continue
        after = api.delete_invoice(str(raw["id"]))
        removed.append({"number": raw.get("number"), "amount": raw.get("amount"),
                        "status": STATUS_NAMES.get(str(raw.get("status_id", "")), str(raw.get("status_id"))),
                        "state_before": record_state(raw), "number_after": after.get("number")})
    left = [r.get("number") for r in api.raw_invoices() if r.get("client_id") == client_id and not r.get("is_deleted")]
    if left:
        raise InvoiceNinjaError(f"invoices for {client_name!r} still not deleted after cleanup: {left}")
    return {"client": client_name, "removed": removed, "remaining_not_deleted": 0}
