"""Tier-5 scripted read-back for the business profile (TimeCamp, Insightly, Invoice Ninja).

The Play image has no root and the owner has configured no API tokens, so the
verifier reads the apps' own screens through uiautomator:

- TimeCamp: the Reports tab, "This Week", lists one row per project with its
  total ("Northwind Traders, 4h 15m").
- Insightly: the organisation record's Description field holds the rate
  ("Hourly rate: 95 USD per hour"); the record is reached through search.
- Invoice Ninja: the Invoices list rows ("Client\\n$403.75\\n0001 • 10/02/2026\\nDraft",
  with a fifth line "Archived"/"Deleted" for non-active records, whose number
  also gets a "_Deleted" suffix) plus, for invoices not seen before, the
  detail view's line items ("Consulting hours\\n$403.75\\n4.25 x $95.00").
  The list is always read with the Filter sheet set to Active + Archived +
  Deleted, so pre and post reads see the same population whatever the agent
  did to the filter.

Parsing is pure and unit-tested; BizReadback drives the device and is only
exercised on the emulator. Coordinates are those of the pixel_7 profile
(1080x2400) on snapshot `business`. All three apps keep their data on the
vendor's servers, so a snapshot restore does NOT undo an invoice the agent
created; the verifier always diffs pre and post invoice numbers.
Never imported by harness/agent.
"""

from __future__ import annotations

import logging
import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field

from harness.contracts import DeviceError
from harness.device.adb_shell import adb
from harness.device.inspect import DEFAULT_MARKOR_DIR, DeviceState, Inspector

log = logging.getLogger(__name__)

TIMECAMP = "com.timecamp.mobile"
INSIGHTLY = "com.insightly.droid"
INVOICE_NINJA = "com.invoiceninja.app"
APPS = (TIMECAMP, INSIGHTLY, INVOICE_NINJA)

# Fixed data on snapshot `business` (see apps-probe/PROBE.md, "Business snapshot").
CLIENT_ORG = "Northwind Traders"
DECOY_ORG = "Northwind Logistics"
ORGS_TO_READ = (CLIENT_ORG,)  # the decoy's rate is never checked; reading it cost ~30 s per run

# Taps, in real pixels, valid for the 1080x2400 pixel_7 profile.
TIMECAMP_REPORTS_TAB = (324, 2276)
INSIGHTLY_SEARCH = (765, 199)
IN_SIDEBAR = (73, 210)
IN_SIDEBAR_INVOICES = (300, 997)
IN_BACK = (73, 210)
IN_FILTER_BUTTON = (326, 2273)
IN_STATES = ("Active", "Archived", "Deleted")  # Filter sheet checkboxes

INVOICE_STATUSES = ("Draft", "Sent", "Partial", "Paid", "Unpaid", "Overdue", "Cancelled", "Reversed")

_DURATION = re.compile(r"^(\d+)h (\d+)m$")
_REPORT_ROW = re.compile(r"^(?P<name>.+?)(?:, (?P<parent>[^,]+))?, (?P<h>\d+)h (?P<m>\d+)m$")
_INVOICE_META = re.compile(r"^(?P<number>\S+)(?: • (?P<date>.+))?$")
_LINE_ITEM = re.compile(r"^(?P<name>.*)\n(?P<total>[^\n]+)\n(?P<qty>[\d.,]+) x (?P<unit>[^\n]+)$")
_NUMBER = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_RATE_LABELLED = re.compile(r"(?i)hourly rate\D{0,12}?" + _NUMBER)
_RATE_PER_HOUR = re.compile(r"(?i)\$?\s*" + _NUMBER + r"\s*(?:USD)?\s*(?:/|per)\s*h(?:ou)?r")
_INVOICE_NUMBER = re.compile(r"^(?=.*\d)[\w.\-/]+$")
DUMP_RETRIES = 3
MAX_LIST_PAGES = 10
SCREEN_TIMEOUT_S = 30.0  # how long a screen may take to appear
FIRST_SCREEN_TIMEOUT_S = 60.0  # the first screen after a launch (cold start after a snapshot restore)


# --- Records -----------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    name: str
    total: float
    quantity: float
    unit_cost: float


@dataclass(frozen=True)
class Invoice:
    number: str
    client: str
    amount: float
    status: str  # Draft, Sent, Paid, ...
    date: str = ""
    items: tuple[LineItem, ...] | None = None  # None: detail view not read
    state: str = "Active"  # Active, Archived or Deleted (record state, distinct from status)


@dataclass
class BizState:
    """What the three apps show. project_hours: TimeCamp project -> hours this
    week. org_descriptions: Insightly organisation -> Description text ("" when
    empty, absent when the organisation was not found). invoices: number -> Invoice."""

    project_hours: dict[str, float] = field(default_factory=dict)
    org_descriptions: dict[str, str] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    invoices_complete: bool = True  # False when the list scan hit MAX_LIST_PAGES


@dataclass
class BizDeviceState(DeviceState):
    biz: BizState = field(default_factory=BizState)


# --- Pure parsers ------------------------------------------------------------


def parse_duration(text: str) -> float | None:
    """'4h 15m' -> 4.25; anything else -> None."""
    m = _DURATION.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) + int(m.group(2)) / 60


def parse_timecamp_report(descs: list[str]) -> dict[str, float]:
    """Project rows of the Reports tab. Sub-tasks appear as 'Task, PARENT, 5h 0m'
    and are keyed by the task name; the 'Total time' row is skipped."""
    hours: dict[str, float] = {}
    for d in descs:
        m = _REPORT_ROW.match(d.strip())
        if not m or m.group("name") == "Total time":
            continue
        hours[m.group("name")] = int(m.group("h")) + int(m.group("m")) / 60
    return hours


_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_COMMA_DECIMAL = re.compile(r"\d+,\d{1,2}")


def parse_number(token: str) -> float | None:
    """One numeric token: '403.75', '1,250.00' (grouped), '4,25' (comma
    decimal), '$95' (currency stripped). None when nothing numeric remains."""
    t = token.strip()
    if _GROUPED.fullmatch(re.sub(r"[^\d.,]", "", t)):
        t = t.replace(",", "")
    elif _COMMA_DECIMAL.fullmatch(re.sub(r"[^\d,]", "", t)) and "." not in t:
        t = t.replace(",", ".")
    cleaned = re.sub(r"[^\d.\-]", "", t)
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def parse_money(text: str) -> float | None:
    """'$403.75' -> 403.75, '$1,250.00' -> 1250.0, '4,25' (comma decimal) -> 4.25."""
    return parse_number(text)


def parse_rate(description: str | None) -> float | None:
    """Hourly rate from free text: 'Hourly rate: 95 USD per hour', '$95/hr'."""
    if not description:
        return None
    m = _RATE_LABELLED.search(description) or _RATE_PER_HOUR.search(description)
    return float(m.group(1).replace(",", "")) if m else None


def parse_invoice_row(desc: str) -> Invoice | None:
    """'Client\\n$403.75\\n0001 • 10/02/2026\\nDraft' -> Invoice (items unread)."""
    lines = [l.strip() for l in desc.split("\n") if l.strip()]
    if len(lines) < 3:
        return None
    amount = parse_money(lines[1])
    meta = _INVOICE_META.match(lines[2])
    if amount is None or not meta or not _INVOICE_NUMBER.match(meta.group("number")):
        return None
    status = lines[3] if len(lines) > 3 else ""
    state = lines[4] if len(lines) > 4 and lines[4] in IN_STATES else "Active"
    return Invoice(number=meta.group("number"), client=lines[0], amount=amount, status=status,
                   date=meta.group("date") or "", state=state)


def parse_line_item(desc: str) -> LineItem | None:
    m = _LINE_ITEM.match(desc.strip())
    if not m:
        return None
    total, qty, unit = parse_money(m.group("total")), parse_money(m.group("qty")), parse_money(m.group("unit"))
    if None in (total, qty, unit):
        return None
    return LineItem(m.group("name"), total, qty, unit)


def parse_detail_items(descs: list[str]) -> tuple[LineItem, ...]:
    return tuple(i for i in (parse_line_item(d) for d in descs) if i is not None)


def detail_status(descs: list[str]) -> str:
    return next((d for d in descs if d in INVOICE_STATUSES), "")


def description_from_descs(descs: list[str]) -> str | None:
    """'Description\\n<text>' node of an Insightly record, or None when absent."""
    for d in descs:
        if d.startswith("Description\n"):
            return d.split("\n", 1)[1].strip()
        if d == "Description":
            return ""
    return None


def parse_dump(xml_text: str) -> list[dict]:
    """uiautomator XML -> [{text, desc, bounds:(x1,y1,x2,y2)}] for nodes with text or desc."""
    start = xml_text.find("<hierarchy")
    end = xml_text.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise DeviceError("uiautomator dump returned no hierarchy")
    try:
        root = ET.fromstring(xml_text[start:end + len("</hierarchy>")])
    except ET.ParseError as exc:
        raise DeviceError(f"uiautomator dump is not well-formed: {exc}") from exc
    out = []
    for n in root.iter("node"):
        a = n.attrib
        text, desc = a.get("text", ""), a.get("content-desc", "")
        if not text and not desc:
            continue
        nums = [int(x) for x in re.findall(r"-?\d+", a.get("bounds", ""))]
        out.append({"text": text, "desc": desc, "bounds": tuple(nums) if len(nums) == 4 else (0, 0, 0, 0),
                    "checked": a.get("checked") == "true"})
    return out


# --- Device driver -----------------------------------------------------------


class ScreenTimeout(DeviceError):
    """wait_for() gave up: the expected screen never appeared."""


class BizReadback:
    """Drives the three apps over adb and returns parsed screen content.
    Every reader launches the app fresh, reads, then force-stops it."""

    def __init__(self, serial: str, settle_s: float = 1.0) -> None:
        self.serial = serial
        self.settle_s = settle_s

    def _shell(self, *args: str, timeout: float = 60.0) -> str:
        return adb(self.serial, "shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)

    def nodes(self) -> list[dict]:
        """One uiautomator dump, retried: 'could not get idle state' is a
        common transient while a screen is still animating."""
        last: Exception | None = None
        for attempt in range(DUMP_RETRIES):
            try:
                return parse_dump(adb(self.serial, "exec-out", "uiautomator", "dump", "/dev/tty", timeout=90))
            except DeviceError as exc:
                last = exc
                if attempt + 1 < DUMP_RETRIES:
                    self._sleep(1.5 * (attempt + 1))
        raise DeviceError(f"uiautomator dump failed {DUMP_RETRIES} times: {last}")

    def descs(self) -> list[str]:
        return [n["desc"] for n in self.nodes() if n["desc"]]

    def tap(self, x: int, y: int) -> None:
        self._shell("input", "tap", str(x), str(y))

    def tap_node(self, node: dict) -> None:
        x1, y1, x2, y2 = node["bounds"]
        self.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def type_text(self, text: str) -> None:
        self._shell("input", "text", text.replace(" ", "%s"))

    def launch(self, package: str, wait_s: float) -> None:
        self._shell("am", "force-stop", package)
        out = self._shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        if "Events injected: 1" not in out:
            raise DeviceError(f"could not launch {package}: {out.strip()[-200:]}")
        time.sleep(wait_s * self.settle_s)

    def close(self) -> None:
        for p in APPS:
            self._shell("am", "force-stop", p)
        self._shell("input", "keyevent", "KEYCODE_HOME")

    def _sleep(self, s: float) -> None:
        time.sleep(s * self.settle_s)

    def wait_for(self, what: str, present: Callable[[list[dict]], bool], *, retap: tuple[int, int] | None = None,
                 timeout_s: float | None = None) -> list[dict]:
        """Dump until `present(nodes)` holds (a cold start after a snapshot
        restore can take far longer than a warm one); re-tap `retap` between
        polls. Raises DeviceError naming the missing screen after timeout_s
        (module SCREEN_TIMEOUT_S when None)."""
        timeout_s = SCREEN_TIMEOUT_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout_s
        while True:
            nodes = self.nodes()
            if present(nodes):
                return nodes
            if time.monotonic() >= deadline:
                raise ScreenTimeout(f"{what} did not appear within {timeout_s:g}s")
            if retap is not None:
                self.tap(*retap)
            self._sleep(2)

    def timecamp_project_hours(self) -> dict[str, float]:
        self.launch(TIMECAMP, 2)
        self.wait_for("TimeCamp timesheet", lambda ns: any(n["text"] == "Timesheet" for n in ns),
                      timeout_s=FIRST_SCREEN_TIMEOUT_S)
        self.tap(*TIMECAMP_REPORTS_TAB)
        nodes = self.wait_for("TimeCamp Reports tab ('This Week')",
                              lambda ns: any(n["text"] == "This Week" for n in ns) and any(", " in n["desc"] for n in ns),
                              retap=TIMECAMP_REPORTS_TAB)
        return parse_timecamp_report([n["desc"] for n in nodes if n["desc"]])

    def insightly_org_description(self, name: str) -> str | None:
        """Description text of the organisation, '' if empty, None if not found."""
        self.launch(INSIGHTLY, 2)
        self.wait_for("Insightly home", lambda ns: any(n["desc"].startswith("CRM\n") for n in ns),
                      timeout_s=FIRST_SCREEN_TIMEOUT_S)
        self.tap(*INSIGHTLY_SEARCH)
        self.wait_for("Insightly search box", lambda ns: any(n["text"] == "Recently Viewed" or n["desc"] == "Recently Viewed" for n in ns)
                      or any(n["desc"] == "\uf1c0" for n in ns))
        self.type_text(name)
        try:  # results come from the network; poll instead of trusting one dump
            nodes = self.wait_for(f"Insightly search result {name!r}", lambda ns: any(n["desc"] == name for n in ns))
        except ScreenTimeout:
            return None  # only "no such record"; a failing dump still raises
        hit = next(n for n in nodes if n["desc"] == name)
        self.tap_node(hit)
        descs = [n["desc"] for n in self.wait_for(f"Insightly record {name!r}",
                                                   lambda ns: any(n["desc"] == f"Organization Name\n{name}" for n in ns))]
        return description_from_descs(descs) or ""

    def _open_invoice_list(self, first: bool) -> list[dict]:
        """Sidebar -> Invoices, scrolled to the top. On the first open of a read
        the Filter sheet is set to Active + Archived + Deleted."""
        if first:
            self.launch(INVOICE_NINJA, 2)
            self.wait_for("Invoice Ninja shell", lambda ns: any(n["desc"] == "Menu Sidebar" for n in ns),
                          timeout_s=FIRST_SCREEN_TIMEOUT_S)
        self.tap(*IN_SIDEBAR)
        self.wait_for("Invoice Ninja sidebar", lambda ns: any(n["desc"] == "Invoices" for n in ns), retap=IN_SIDEBAR)
        self.tap(*IN_SIDEBAR_INVOICES)
        nodes = self.wait_for("Invoice Ninja invoices list", lambda ns: any(n["desc"] == "New Invoice" for n in ns))
        return self._ensure_all_states() if first else nodes

    def _ensure_all_states(self) -> list[dict]:
        """Tick every record-state checkbox in the Filter sheet, then close it."""
        self.tap(*IN_FILTER_BUTTON)
        nodes = self.wait_for("Invoice Ninja filter sheet",
                              lambda ns: all(any(n["desc"] == st for n in ns) for st in IN_STATES))
        for st in IN_STATES:
            box = next(n for n in nodes if n["desc"] == st)
            if not box["checked"]:
                self.tap_node(box)
                nodes = self.wait_for(f"filter {st} ticked",
                                      lambda ns, st=st: any(n["desc"] == st and n["checked"] for n in ns))
        self.tap(*IN_FILTER_BUTTON)
        return self.wait_for("Invoice Ninja invoices list (filter closed)",
                             lambda ns: any(n["desc"] == "New Invoice" for n in ns)
                             and not any(n["desc"] == "Archived" and "checked" in n for n in ns if n["desc"] in IN_STATES))

    def _visible_rows(self, nodes: list[dict]) -> list[tuple[dict, Invoice]]:
        return [(n, inv) for n in nodes if "\n" in n["desc"] for inv in [parse_invoice_row(n["desc"])] if inv]

    def _scan_pages(self, nodes: list[dict], invoices: dict[str, Invoice], known: set[str] | None
                    ) -> tuple[tuple[dict, Invoice] | None, bool]:
        """Walk the list from the given screen, recording rows that need no
        detail view. Returns (first row needing a detail view, scan complete)."""
        for _ in range(MAX_LIST_PAGES):
            rows = self._visible_rows(nodes)
            for node, inv in rows:
                if inv.number in invoices:
                    continue
                if known is not None and inv.number not in known:
                    return (node, inv), False
                invoices[inv.number] = inv
            if len(rows) < 2:
                return None, True
            first, last = rows[0][0]["bounds"], rows[-1][0]["bounds"]
            self._shell("input", "swipe", "540", str(last[3] - 10), "540", str(first[1] + 10), "400")
            self._sleep(2)
            nodes = self.nodes()
            if {i.number for _, i in self._visible_rows(nodes)} <= {i.number for _, i in rows}:
                return None, True  # the page did not move: end of list
        return None, False

    def invoice_ninja_invoices(self, known: set[str] | None = None) -> tuple[dict[str, Invoice], bool]:
        """All invoices (Active, Archived and Deleted), scrolled until the list
        ends. The detail view (line items, status) is opened for every number
        not in `known`; None skips all detail views. After each detail view the
        list is re-opened from the top and re-scanned, so stale row positions
        are never tapped. Returns (invoices, complete)."""
        nodes = self._open_invoice_list(first=True)
        invoices: dict[str, Invoice] = {}
        for _ in range(MAX_LIST_PAGES * 4):
            pending, complete = self._scan_pages(nodes, invoices, known)
            if pending is None:
                return invoices, complete
            node, inv = pending
            self.tap_node(node)
            descs = [n["desc"] for n in self.wait_for(f"invoice {inv.number} detail",
                                                       lambda ns, num=inv.number: any(n["desc"] == num for n in ns))]
            invoices[inv.number] = Invoice(inv.number, inv.client, inv.amount, detail_status(descs) or inv.status,
                                           inv.date, parse_detail_items(descs), inv.state)
            self.tap(*IN_BACK)
            nodes = self._open_invoice_list(first=False)
        return invoices, False


class BusinessInspector(Inspector):
    """Inspector for the business profile: the usual device state plus a
    BizState read back from the apps.

    TimeCamp hours and Insightly descriptions are read once, on the first
    snapshot_state() call (the pre-state), and reused afterwards: they are
    the fixed source data of the snapshot and the tasks only read them.
    Invoices are re-read every call; detail views are opened only for
    numbers not present in the first read, so the pre-state stays cheap.
    """

    def __init__(self, serial: str, markor_dir: str = DEFAULT_MARKOR_DIR,
                 readback: BizReadback | None = None) -> None:
        super().__init__(serial, markor_dir)
        self.readback = readback or BizReadback(serial)
        self._sources: tuple[dict[str, float], dict[str, str]] | None = None
        self._first_invoices: set[str] | None = None

    def snapshot_state(self) -> BizDeviceState:
        base = super().snapshot_state()
        try:
            if self._sources is None:
                hours = self.readback.timecamp_project_hours()
                descs = {}
                for org in ORGS_TO_READ:
                    d = self.readback.insightly_org_description(org)
                    if d is not None:
                        descs[org] = d
                self._sources = (hours, descs)
            hours, descs = self._sources
            invoices, complete = self.readback.invoice_ninja_invoices(known=self._first_invoices)
            if self._first_invoices is None:
                self._first_invoices = set(invoices)
        finally:
            self.readback.close()
        return BizDeviceState(
            contacts=base.contacts, sms=base.sms, events=base.events, files=base.files,
            device_time_offset_ms=base.device_time_offset_ms,
            biz=BizState(project_hours=dict(hours), org_descriptions=dict(descs), invoices=invoices,
                         invoices_complete=complete),
        )
