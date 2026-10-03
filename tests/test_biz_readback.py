"""Pure parsers of the business read-back and the inspector's caching rules."""

import pytest

from harness.contracts import DeviceError
from harness.verify import biz_readback as rb
from harness.verify.biz_readback import BusinessInspector, Invoice, LineItem

REPORT_DESCS = [
    "Total time, 11h 15m",
    "Ideation, ANDROID FLOW, 5h 0m",
    "Northwind Traders, 4h 15m",
    "Android Flow, 2h 0m",
    ", Timesheet",
]


def test_parse_duration():
    assert rb.parse_duration("4h 15m") == 4.25
    assert rb.parse_duration("0h 0m") == 0.0
    assert rb.parse_duration("7:00") is None
    assert rb.parse_duration("") is None


def test_parse_timecamp_report_keys_projects_and_subtasks_and_skips_total():
    assert rb.parse_timecamp_report(REPORT_DESCS) == {"Ideation": 5.0, "Northwind Traders": 4.25, "Android Flow": 2.0}


def test_parse_timecamp_report_handles_empty_week():
    assert rb.parse_timecamp_report(["Total time, 0h 0m", "What are you working on?"]) == {}


@pytest.mark.parametrize("text, rate", [
    ("Hourly rate: 95 USD per hour", 95.0),
    ("hourly rate - $120.50", 120.5),
    ("$175/hr", 175.0),
    ("Rate 80 USD per hour", 80.0),
    ("Hourly rate: $1,200 per hour", 1200.0),
    ("No rate agreed yet", None),
    ("", None),
    (None, None),
])
def test_parse_rate(text, rate):
    assert rb.parse_rate(text) == rate


def test_parse_money():
    assert rb.parse_money("$403.75") == 403.75
    assert rb.parse_money("$1,250.00") == 1250.0
    assert rb.parse_money("-$5.00") == -5.0
    assert rb.parse_money("n/a") is None
    assert rb.parse_money("4,25") == 4.25 and rb.parse_money("1,250") == 1250.0
    assert rb.parse_number("$1,140.00") == 1140.0 and rb.parse_number("95") == 95.0 and rb.parse_number("x") is None


def test_parse_invoice_row():
    inv = rb.parse_invoice_row("Northwind Traders\n$403.75\n0001 • 10/02/2026\nDraft")
    assert inv == Invoice("0001", "Northwind Traders", 403.75, "Draft", "10/02/2026", None)
    assert rb.parse_invoice_row("Northwind Traders\n$0.00\n0002") == Invoice("0002", "Northwind Traders", 0.0, "", "", None)
    deleted = rb.parse_invoice_row("Northwind Traders\n$403.75\n0001_Deleted • 10/02/2026\nDraft\nDeleted")
    assert deleted == Invoice("0001_Deleted", "Northwind Traders", 403.75, "Draft", "10/02/2026", None, "Deleted")
    assert rb.parse_invoice_row("X\n$1.00\n0003 • 1/1/2026\nSent\nArchived").state == "Archived"
    assert rb.parse_invoice_row("Show Table") is None
    assert rb.parse_invoice_row("Northwind Traders\n$0.00\nDraft") is None  # no number line: not a row
    assert rb.parse_invoice_row("Client\nno money\n0003 • 1/1/2026\nSent") is None


def test_parse_line_item_and_detail():
    item = rb.parse_line_item("Consulting hours\n$403.75\n4.25 x $95.00")
    assert item == LineItem("Consulting hours", 403.75, 4.25, 95.0)
    assert rb.parse_line_item("Invoice Amount\n$403.75") is None
    descs = ["0001", "Draft", "Invoice Amount\n$403.75", "Consulting hours\n$403.75\n4.25 x $95.00",
             "Travel\n$10.00\n1 x $10.00", "Subtotal\n$413.75", "Send Email"]
    assert rb.parse_detail_items(descs) == (LineItem("Consulting hours", 403.75, 4.25, 95.0),
                                            LineItem("Travel", 10.0, 1.0, 10.0))
    assert rb.detail_status(descs) == "Draft"
    assert rb.detail_status(["0001", "Invoice Amount\n$1.00"]) == ""


def test_description_from_descs():
    assert rb.description_from_descs(["Record ID\n1", "Description\nHourly rate: 95 USD per hour"]) == "Hourly rate: 95 USD per hour"
    assert rb.description_from_descs(["Description"]) == ""
    assert rb.description_from_descs(["Record ID\n1"]) is None


XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0"><node text="" content-desc="" bounds="[0,0][1080,2400]">
<node text="This Week" content-desc="" bounds="[29,350][231,401]"/>
<node text="" content-desc="Northwind Traders, 4h 15m" bounds="[0,1567][1080,1698]"/>
<node text="" content-desc="edge" bounds="[-40,0][1040,120]"/>
<node text="" content-desc="Active" checked="true" checkable="true" bounds="[0,1828][1080,1954]"/></node></hierarchy>"""


def test_parse_dump_keeps_only_labelled_nodes_with_bounds():
    nodes = rb.parse_dump("UI hierchary dumped to: /dev/tty\n" + XML)
    assert nodes == [
        {"text": "This Week", "desc": "", "bounds": (29, 350, 231, 401), "checked": False},
        {"text": "", "desc": "Northwind Traders, 4h 15m", "bounds": (0, 1567, 1080, 1698), "checked": False},
        {"text": "", "desc": "edge", "bounds": (-40, 0, 1040, 120), "checked": False},
        {"text": "", "desc": "Active", "bounds": (0, 1828, 1080, 1954), "checked": True},
    ]
    with pytest.raises(DeviceError):
        rb.parse_dump("ERROR: could not get idle state.")
    with pytest.raises(DeviceError, match="not well-formed"):
        rb.parse_dump("<hierarchy><node text='x' bounds='[0,0][1,1]'></hierarchy>")


class ScriptedReadback:
    """Stands in for BizReadback; records which detail views were requested."""

    def __init__(self):
        self.calls = []
        self.listing = {"0001": Invoice("0001", "ZZ", 1.0, "Draft")}
        self.closed = 0

    def timecamp_project_hours(self):
        self.calls.append("timecamp")
        return {"Northwind Traders": 4.25}

    def insightly_org_description(self, name):
        self.calls.append(("insightly", name))
        return {rb.CLIENT_ORG: "Hourly rate: 95 USD per hour", rb.DECOY_ORG: ""}.get(name)

    def invoice_ninja_invoices(self, known=None):
        self.calls.append(("invoices", None if known is None else sorted(known)))
        return dict(self.listing), True

    def close(self):
        self.closed += 1


def test_business_inspector_reads_sources_once_and_details_only_new_invoices(monkeypatch):
    from harness.device.inspect import DeviceState, Inspector

    monkeypatch.setattr(Inspector, "snapshot_state", lambda self: DeviceState())
    scripted = ScriptedReadback()
    insp = BusinessInspector("emulator-0000", readback=scripted)
    pre = insp.snapshot_state()
    assert pre.biz.project_hours == {"Northwind Traders": 4.25}
    assert pre.biz.org_descriptions == {rb.CLIENT_ORG: "Hourly rate: 95 USD per hour"}
    assert pre.biz.invoices.keys() == {"0001"} and pre.biz.invoices["0001"].items is None
    assert scripted.calls == ["timecamp", ("insightly", rb.CLIENT_ORG), ("invoices", None)]
    scripted.listing["0002"] = Invoice("0002", "Northwind Traders", 403.75, "Draft")
    scripted.calls.clear()
    post = insp.snapshot_state()
    assert scripted.calls == [("invoices", ["0001"])]  # sources cached; detail wanted for anything but 0001
    assert post.biz.project_hours == {"Northwind Traders": 4.25}
    assert post.biz.org_descriptions == pre.biz.org_descriptions
    assert set(post.biz.invoices) == {"0001", "0002"} and post.biz.invoices_complete is True
    assert scripted.closed == 2


def test_business_inspector_second_snapshot_failure_propagates_and_closes(monkeypatch):
    from harness.device.inspect import DeviceState, Inspector

    monkeypatch.setattr(Inspector, "snapshot_state", lambda self: DeviceState())
    scripted = ScriptedReadback()
    insp = BusinessInspector("emulator-0000", readback=scripted)
    insp.snapshot_state()

    def broken(known=None):
        raise DeviceError("Invoice Ninja invoices list did not appear within 30s")

    scripted.invoice_ninja_invoices = broken
    with pytest.raises(DeviceError, match="invoices list"):
        insp.snapshot_state()
    assert scripted.closed == 2


def test_business_inspector_closes_apps_when_readback_fails(monkeypatch):
    from harness.device.inspect import DeviceState, Inspector

    monkeypatch.setattr(Inspector, "snapshot_state", lambda self: DeviceState())
    scripted = ScriptedReadback()

    def boom():
        raise DeviceError("Reports tab missing")

    scripted.timecamp_project_hours = boom
    insp = BusinessInspector("emulator-0000", readback=scripted)
    with pytest.raises(DeviceError):
        insp.snapshot_state()
    assert scripted.closed == 1


def test_business_inspector_reads_invoices_from_the_api_and_never_opens_the_app(monkeypatch):
    from harness.device.inspect import DeviceState, Inspector
    from harness.verify.biz_api import InvoiceNinjaApi
    from tests.biz_fakes import API_ROOT, FakeInvoiceServer

    monkeypatch.setattr(Inspector, "snapshot_state", lambda self: DeviceState())
    scripted = ScriptedReadback()
    server = FakeInvoiceServer([{"id": "nw", "name": "Northwind Traders"}], [
        {"id": "a", "number": "0001_Deleted", "client_id": "nw", "amount": 403.75, "status_id": "1",
         "is_deleted": True, "archived_at": 1, "line_items": []}])
    insp = BusinessInspector("emulator-0000", readback=scripted, invoice_api=InvoiceNinjaApi(API_ROOT, "t", opener=server))
    pre = insp.snapshot_state()
    assert pre.biz.invoices["0001_Deleted"].state == "Deleted" and pre.biz.invoices_complete
    server.invoices.append({"id": "b", "number": "0003", "client_id": "nw", "amount": 403.75, "status_id": "1",
                            "is_deleted": False, "archived_at": None,
                            "line_items": [{"product_key": "Consulting", "quantity": 4.25, "cost": 95, "line_total": 403.75}]})
    post = insp.snapshot_state()
    assert post.biz.invoices["0003"].items == (LineItem("Consulting", 403.75, 4.25, 95.0),)  # items without a detail view
    assert post.biz.project_hours == {"Northwind Traders": 4.25}
    assert scripted.calls == ["timecamp", ("insightly", rb.CLIENT_ORG)]  # no screen invoice read at all
    assert scripted.closed == 2


def test_business_inspector_api_failure_raises_instead_of_falling_back_to_the_screen(monkeypatch):
    from harness.device.inspect import DeviceState, Inspector
    from harness.verify.biz_api import InvoiceNinjaApi, InvoiceNinjaError
    from tests.biz_fakes import API_ROOT, FakeInvoiceServer

    monkeypatch.setattr(Inspector, "snapshot_state", lambda self: DeviceState())
    scripted = ScriptedReadback()
    server = FakeInvoiceServer([], [], fail={"/api/v1/clients": 401})
    insp = BusinessInspector("emulator-0000", readback=scripted, invoice_api=InvoiceNinjaApi(API_ROOT, "t", opener=server))
    with pytest.raises(InvoiceNinjaError, match="HTTP 401"):
        insp.snapshot_state()
    assert not any(c[0] == "invoices" for c in scripted.calls if isinstance(c, tuple))
    assert scripted.closed == 1


# --- Driver paths, with the device replaced by scripted screens -------------------


def _n(desc="", text="", bounds=(0, 0, 100, 100), checked=False):
    return {"text": text, "desc": desc, "bounds": bounds, "checked": checked}


class ScreenReadback(rb.BizReadback):
    """BizReadback whose device is a list of screens: every nodes() call pops
    the next screen (the last one repeats). Taps, launches and typing are logged."""

    def __init__(self, screens):
        super().__init__("emulator-0000", settle_s=0.0)
        self.screens = list(screens)
        self.log = []

    def nodes(self):
        if len(self.screens) > 1:
            return self.screens.pop(0)
        return self.screens[0]

    def launch(self, package, wait_s):
        self.log.append(("launch", package))

    def tap(self, x, y):
        self.log.append(("tap", x, y))

    def type_text(self, text):
        self.log.append(("type", text))

    def _shell(self, *args, timeout=60.0):
        self.log.append(("shell",) + args)
        return ""


# TimeCamp screens as dumped live on 2026-10-03 (Reports -> Custom -> date pickers).
TC_HOME = [_n(text="Timesheet")]
TC_CHIPS = [_n(text="This Week", bounds=(29, 350, 231, 401)), _n(text="Custom", bounds=(849, 350, 1051, 401))]
TC_OK = _n("Ok", bounds=(540, 1337, 1054, 1402))
PREV, NEXT, PICKER_OK = (261, 921), (819, 921), (802, 1802)


def tc_dialog(start, end):
    return TC_CHIPS + [_n(text="Select a dates range"), _n(start, bounds=(79, 1211, 514, 1290)),
                       _n(end, bounds=(567, 1211, 1002, 1290)), TC_OK]


def day_xy(day):
    return 50, 1005 + 10 * day


def picker(month, year=2026, checked=None):
    """Android date picker: OK, month arrows and one cell per day ('28 September 2026')."""
    days = [_n(f"{d:02d} {month} {year}", text=str(d), bounds=(0, 1000 + 10 * d, 100, 1010 + 10 * d),
               checked=d == checked) for d in range(1, 31)]
    return [_n(text="OK", bounds=(718, 1731, 886, 1873)), _n("Previous month", bounds=(198, 858, 324, 984)),
            _n("Next month", bounds=(756, 858, 882, 984))] + days


TC_REPORT = TC_CHIPS + [_n(text="28.09-03.10 2026"), _n("Total time, 11h 15m"), _n("Northwind Traders, 4h 15m"),
                        _n("Android Flow, 2h 0m")]
TC_FIXED_RANGE = [
    TC_HOME, TC_CHIPS,
    tc_dialog("2026-10-02", "2026-10-02"), picker("October"), picker("September"), picker("September", checked=28),
    tc_dialog("2026-09-28", "2026-10-02"), picker("October"), picker("October", checked=3),
    tc_dialog("2026-09-28", "2026-10-03"), TC_REPORT,
]


def test_date_helpers_match_the_strings_timecamp_and_the_picker_show():
    from datetime import date

    assert rb.range_phrase() == "28 September and 3 October 2026"
    assert rb.range_phrase(date(2025, 12, 29), date(2026, 1, 3)) == "29 December 2025 and 3 January 2026"
    assert rb.report_header(rb.REPORT_START, rb.REPORT_END) == "28.09-03.10 2026"
    assert rb.report_header(date(2026, 9, 7), date(2026, 9, 8)) == "07-08 Sep 2026"  # same month, seen live
    assert rb.report_header(date(2026, 8, 31), date(2026, 9, 1)) == "31.08-01.09 2026"
    assert rb.picker_label(date(2026, 10, 3)) == "03 October 2026"
    assert rb.picker_month([n["desc"] for n in picker("September")]) == (2026, 9)
    assert rb.picker_month(["01 October 2026", "30 September 2026", "02 October 2026"]) == (2026, 10)
    assert rb.picker_month(["OK", "Previous month"]) is None


def test_timecamp_reader_selects_the_fixed_range_and_reads_its_report():
    r = ScreenReadback(TC_FIXED_RANGE)
    assert r.timecamp_project_hours() == {"Northwind Traders": 4.25, "Android Flow": 2.0}
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert taps == [rb.TIMECAMP_REPORTS_TAB, (950, 375),  # Reports, Custom
                    (296, 1250), PREV, day_xy(28), PICKER_OK,  # From: back one month, 28 September
                    (784, 1250), day_xy(3), PICKER_OK,  # To: 3 October is on the opening page
                    (797, 1369)]  # Ok


def test_timecamp_reader_pages_forward_for_a_later_month_and_reads_an_empty_range():
    from datetime import date

    empty = TC_CHIPS + [_n(text="02-03 Nov 2026"), _n(text="Please fill your timesheet to see reports for"),
                        _n(text="02-03 Nov 2026")]
    r = ScreenReadback([TC_HOME, TC_CHIPS, tc_dialog("2026-10-02", "2026-10-02"), picker("October"),
                        picker("November"), picker("November", checked=2),
                        tc_dialog("2026-11-02", "2026-10-02"), picker("October"), picker("November"),
                        picker("November", checked=3), tc_dialog("2026-11-02", "2026-11-03"), empty])
    assert r.timecamp_project_hours(date(2026, 11, 2), date(2026, 11, 3)) == {}
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert taps.count(NEXT) == 2 and PREV not in taps


def test_timecamp_reader_waits_until_the_rows_read_the_same_twice():
    """Rows carry no range: a first dump that already has the new header but
    old rows must not be returned."""
    stale_rows = TC_CHIPS + [_n(text="28.09-03.10 2026"), _n("Total time, 0h 30m"), _n("Northwind Traders, 0h 30m")]
    r = ScreenReadback(TC_FIXED_RANGE[:-1] + [stale_rows, TC_REPORT])
    assert r.timecamp_project_hours() == {"Northwind Traders": 4.25, "Android Flow": 2.0}


def test_timecamp_reader_rejects_an_empty_state_for_another_range(monkeypatch):
    from datetime import date

    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    stale = TC_CHIPS + [_n(text="07-08 Sep 2026"), _n(text="Please fill your timesheet to see reports for"),
                        _n(text="03-03 Oct 2026")]  # header updated, empty state still the opening range
    r = ScreenReadback([TC_HOME, TC_CHIPS, tc_dialog("2026-10-02", "2026-10-02"), picker("September"),
                        picker("September", checked=7), tc_dialog("2026-09-07", "2026-10-02"), picker("September"),
                        picker("September", checked=8), tc_dialog("2026-09-07", "2026-09-08"), stale])
    with pytest.raises(DeviceError, match="07-08 Sep 2026"):
        r.timecamp_project_hours(date(2026, 9, 7), date(2026, 9, 8))


def test_timecamp_reader_does_not_accept_a_report_for_another_period(monkeypatch):
    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    stale = TC_CHIPS + [_n(text="27.09-03.10 2026"), _n("Northwind Traders, 4h 15m"), _n("Total time, 11h 15m")]
    r = ScreenReadback(TC_FIXED_RANGE[:-1] + [stale])
    with pytest.raises(DeviceError, match="28.09-03.10 2026"):
        r.timecamp_project_hours()


def test_pick_date_pages_forward_across_a_year_boundary():
    from datetime import date

    r = ScreenReadback([picker("December", year=2025), picker("January"), picker("January", checked=3)])
    r._pick_date(_n("2025-12-01", bounds=(79, 1211, 514, 1290)), date(2026, 1, 3))
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert taps == [(296, 1250), NEXT, day_xy(3), PICKER_OK]


def test_pick_date_raises_when_the_day_never_shows_selected(monkeypatch):
    from datetime import date

    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    r = ScreenReadback([picker("October")])  # tapping 3 October never ticks it
    with pytest.raises(DeviceError, match="03 October 2026 selected"):
        r._pick_date(_n("2026-10-02"), date(2026, 10, 3))
    assert ("tap",) + PICKER_OK not in r.log  # OK is never pressed on an unconfirmed day


def test_pick_date_raises_when_the_picker_disappears_while_paging():
    from datetime import date

    r = ScreenReadback([picker("October"), [_n(text="OK")]])
    with pytest.raises(DeviceError, match="date picker lost while paging to 28 September 2026"):
        r._pick_date(_n("2026-10-02"), date(2026, 9, 28))


def test_timecamp_reader_raises_when_the_custom_chip_or_a_day_never_appears(monkeypatch):
    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    with pytest.raises(DeviceError, match="Reports tab"):
        ScreenReadback([TC_HOME, [_n(text="Timesheet")]]).timecamp_project_hours()
    monkeypatch.setattr(rb, "MAX_MONTH_STEPS", 3)
    r = ScreenReadback([TC_HOME, TC_CHIPS, tc_dialog("2026-10-02", "2026-10-02"), picker("October", year=2030)])
    with pytest.raises(DeviceError, match="never showed 28 September 2026"):
        r.timecamp_project_hours()
    no_arrows = [n for n in picker("October") if "month" not in n["desc"]]
    r = ScreenReadback([TC_HOME, TC_CHIPS, tc_dialog("2026-10-02", "2026-10-02"), no_arrows])
    with pytest.raises(DeviceError, match="no 'Previous month' button"):
        r.timecamp_project_hours()


IN_HOME = [_n("CRM\nHome")]
IN_SEARCH = [_n("\uf1c0"), _n("Recently Viewed")]


def test_insightly_reader_returns_description_empty_string_and_none(monkeypatch):
    results = [_n("Northwind Traders", bounds=(0, 283, 1080, 430)), _n("Northwind Logistics", bounds=(0, 430, 1080, 577))]
    record = [_n("Organization Name\nNorthwind Traders"), _n("Description\nHourly rate: 95 USD per hour")]
    # Results arrive late: one empty dump first, then the list.
    r = ScreenReadback([IN_HOME, IN_SEARCH, [_n("\uf1c0")], results, record])
    assert r.insightly_org_description("Northwind Traders") == "Hourly rate: 95 USD per hour"
    assert ("tap", 540, 356) in r.log  # the matching row, not the first row blindly
    r = ScreenReadback([IN_HOME, IN_SEARCH, results, [_n("Organization Name\nNorthwind Logistics")]])
    assert r.insightly_org_description("Northwind Logistics") == ""
    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    r = ScreenReadback([IN_HOME, IN_SEARCH, results])
    assert r.insightly_org_description("Contoso") is None


def test_insightly_reader_propagates_dump_failures_instead_of_not_found():
    class Broken(ScreenReadback):
        def nodes(self):
            if self.screens and self.screens[0] == "boom":
                raise DeviceError("uiautomator dump failed 3 times")
            return super().nodes()

    r = Broken([IN_HOME, IN_SEARCH, "boom"])
    with pytest.raises(DeviceError, match="dump failed"):
        r.insightly_org_description("Northwind Traders")


def test_insightly_reader_raises_when_the_record_does_not_open(monkeypatch):
    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    results = [_n("Northwind Traders", bounds=(0, 283, 1080, 430))]
    r = ScreenReadback([IN_HOME, IN_SEARCH, results, [_n("Organization Name\nSomething Else")]])
    with pytest.raises(DeviceError, match="record 'Northwind Traders'"):
        r.insightly_org_description("Northwind Traders")


NIN_SHELL = [_n("Menu Sidebar")]
# Live layout on 2026-10-03: the phone-verification banner pushes Invoices to y 808-934.
NIN_SIDEBAR = [_n("Invoices", bounds=(0, 808, 714, 934)), _n("New Invoice", bounds=(557, 808, 683, 934)),
               _n("Recurring Invoices", bounds=(0, 934, 714, 1086))]
INVOICES_ROW_TAP = ("tap", 357, 871)
ROW1 = _n("ZZ Probe Test Client\n$120.00\n0001 • 10/01/2026\nSent", bounds=(0, 401, 1080, 590))
ROW2 = _n("Northwind Traders\n$403.75\n0002 • 10/02/2026\nDraft", bounds=(0, 594, 1080, 783))
LIST = [_n("New Invoice"), ROW1, ROW2]
DETAIL2 = [_n("0002"), _n("Draft"), _n("Consulting hours\n$403.75\n4.25 x $95.00"), _n("Send Email")]
FILTER_DEFAULT = [_n("New Invoice"), _n("Active", bounds=(0, 1828, 1080, 1954), checked=True),
                  _n("Archived", bounds=(0, 1954, 1080, 2080)), _n("Deleted", bounds=(0, 2080, 1080, 2206))]
FILTER_ARCHIVED = [n if n["desc"] != "Archived" else _n("Archived", bounds=n["bounds"], checked=True) for n in FILTER_DEFAULT]
FILTER_ALL = [n if n["desc"] != "Deleted" else _n("Deleted", bounds=n["bounds"], checked=True) for n in FILTER_ARCHIVED]


def _first_open(list_screen):
    """Screens for launch -> sidebar -> list -> filter sheet (Archived and
    Deleted get ticked) -> list with the widened filter."""
    return [NIN_SHELL, NIN_SIDEBAR, list_screen, FILTER_DEFAULT, FILTER_ARCHIVED, FILTER_ALL, list_screen]


def test_invoice_reader_ticks_archived_and_deleted_once_per_read():
    r = ScreenReadback(_first_open(LIST) + [LIST])
    out, complete = r.invoice_ninja_invoices(known=None)
    assert set(out) == {"0001", "0002"} and complete
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert taps.count(rb.IN_FILTER_BUTTON) == 2  # open and close the sheet
    assert (540, 2017) in taps and (540, 2143) in taps  # Archived and Deleted ticked
    assert (540, 1891) not in taps  # Active was already ticked


def test_invoice_reader_leaves_an_already_widened_filter_alone():
    r = ScreenReadback([NIN_SHELL, NIN_SIDEBAR, LIST, FILTER_ALL, LIST, LIST])
    out, complete = r.invoice_ninja_invoices(known=None)
    assert set(out) == {"0001", "0002"} and complete
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert (540, 2017) not in taps and (540, 2143) not in taps


def test_invoice_reader_opens_details_only_for_unknown_numbers():
    r = ScreenReadback(_first_open(LIST) + [DETAIL2, NIN_SIDEBAR, LIST, LIST])
    out, complete = r.invoice_ninja_invoices(known={"0001"})
    assert complete
    assert out["0001"].items is None and out["0001"].status == "Sent"
    assert out["0002"] == Invoice("0002", "Northwind Traders", 403.75, "Draft", "10/02/2026",
                                  (LineItem("Consulting hours", 403.75, 4.25, 95.0),))
    taps = [t[1:] for t in r.log if t[0] == "tap"]
    assert (540, 688) in taps and (540, 495) not in taps


def test_invoice_reader_detail_status_overrides_list_status_and_keeps_state():
    row = _n("Northwind Traders\n$403.75\n0002 • 10/02/2026\nDraft\nArchived", bounds=(0, 401, 1080, 590))
    detail = [_n("0002"), _n("Sent"), _n("Consulting hours\n$403.75\n4.25 x $95.00")]
    lst = [_n("New Invoice"), row]
    r = ScreenReadback(_first_open(lst) + [detail, NIN_SIDEBAR, lst, lst])
    inv = r.invoice_ninja_invoices(known=set())[0]["0002"]
    assert inv.status == "Sent" and inv.state == "Archived"


def test_invoice_reader_skips_all_details_when_known_is_none_and_handles_empty_list():
    r = ScreenReadback(_first_open(LIST) + [LIST])
    out, _ = r.invoice_ninja_invoices(known=None)
    assert set(out) == {"0001", "0002"} and all(i.items is None for i in out.values())
    empty = [_n("New Invoice"), _n("Click + to create a record")]
    r = ScreenReadback(_first_open(empty) + [empty])
    assert r.invoice_ninja_invoices(known=None) == ({}, True)


def test_invoice_reader_raises_when_list_detail_or_filter_never_opens(monkeypatch):
    monkeypatch.setattr(rb, "SCREEN_TIMEOUT_S", 0.0)
    monkeypatch.setattr(rb, "FIRST_SCREEN_TIMEOUT_S", 0.0)
    with pytest.raises(DeviceError, match="invoices list"):
        ScreenReadback([NIN_SHELL, NIN_SIDEBAR, [_n("Dashboard")]]).invoice_ninja_invoices()
    with pytest.raises(DeviceError, match="filter sheet"):
        ScreenReadback([NIN_SHELL, NIN_SIDEBAR, LIST, LIST]).invoice_ninja_invoices()
    with pytest.raises(DeviceError, match="invoice 0002 detail"):
        ScreenReadback(_first_open(LIST) + [[_n("0001"), _n("Sent")]]).invoice_ninja_invoices(known={"0001"})


def test_invoice_reader_scrolls_until_the_page_stops_moving():
    row3 = _n("Northwind Traders\n$50.00\n0003 • 10/03/2026\nDraft", bounds=(0, 594, 1080, 783))
    page2 = [_n("New Invoice"), ROW2, row3]
    r = ScreenReadback(_first_open(LIST) + [page2, page2])
    out, complete = r.invoice_ninja_invoices(known=None)
    assert set(out) == {"0001", "0002", "0003"} and complete
    swipes = [e for e in r.log if e[0] == "shell" and e[2] == "swipe"]
    assert len(swipes) == 2  # one page turn that found a row, one that found nothing


def test_invoice_reader_reports_incomplete_when_the_list_never_ends(monkeypatch):
    monkeypatch.setattr(rb, "MAX_LIST_PAGES", 2)
    pages = []
    for i in range(1, 6):
        pages.append([_n("New Invoice"), _n(f"C\n$1.00\n{i:04d} • 1/1/2026\nDraft", bounds=(0, 401, 1080, 590)),
                      _n(f"C\n$1.00\n{i + 1:04d} • 1/1/2026\nDraft", bounds=(0, 594, 1080, 783))])
    r = ScreenReadback(_first_open(pages[0]) + pages[1:])
    out, complete = r.invoice_ninja_invoices(known=None)
    assert not complete and len(out) >= 3


def test_invoice_reader_rescans_from_the_top_after_each_detail_view():
    """Two unknown invoices on different pages: after the first detail view the
    list is re-opened from the top and scanned again, so the second one is found."""
    row3 = _n("Northwind Traders\n$50.00\n0003 • 10/03/2026\nDraft", bounds=(0, 594, 1080, 783))
    page1 = [_n("New Invoice"), ROW1, ROW2]
    page2 = [_n("New Invoice"), ROW2, row3]
    detail3 = [_n("0003"), _n("Draft"), _n("Review\n$50.00\n0.5 x $100.00")]
    screens = _first_open(page1) + [DETAIL2, NIN_SIDEBAR, page1, page2, detail3, NIN_SIDEBAR, page1, page2, page2]
    r = ScreenReadback(screens)
    out, complete = r.invoice_ninja_invoices(known={"0001"})
    assert complete and set(out) == {"0001", "0002", "0003"}
    assert out["0002"].items and out["0003"].items == (LineItem("Review", 50.0, 0.5, 100.0),)
    opens = [t for t in r.log if t == INVOICES_ROW_TAP]
    assert len(opens) == 3  # initial open plus one re-open per detail view
    assert ("tap", 357, 1010) not in r.log  # never Recurring Invoices


def test_nodes_retries_transient_dump_failures(monkeypatch):
    attempts = []

    def flaky(serial, *args, timeout=0):
        attempts.append(args)
        if len(attempts) < 3:
            raise DeviceError("ERROR: could not get idle state.")
        return XML

    monkeypatch.setattr(rb, "adb", flaky)
    r = rb.BizReadback("emulator-0000", settle_s=0.0)
    assert len(r.nodes()) == 4 and len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(rb, "adb", lambda *a, **k: (_ for _ in ()).throw(DeviceError("down")))
    with pytest.raises(DeviceError, match="3 times"):
        r.nodes()
