import shlex

import pytest

import harness.device.inspect as ins
from harness.device.inspect import (
    AdbError, Inspector, contacts_from_rows, parse_content_rows, sms_from_rows,
)

SMS_OUT = """\
Row: 0 _id=12, thread_id=3, type=2, date=1790000000123, address=+14155550123, body=Hi, see you at 5, ok?
Row: 1 _id=13, thread_id=NULL, type=3, date=1790000000999, address=NULL, body=line one
line two
Row: 2 _id=14, thread_id=4, type=1, date=0, address=555, body=
"""


def test_parses_rows_with_commas_nulls_and_multiline_values():
    rows = parse_content_rows(SMS_OUT, ins.SMS_COLUMNS)
    assert rows[0]["body"] == "Hi, see you at 5, ok?"
    assert rows[0]["address"] == "+14155550123"
    assert rows[1]["thread_id"] is None and rows[1]["address"] is None
    assert rows[1]["body"] == "line one\nline two"
    assert rows[2]["body"] == ""
    sms = sms_from_rows(rows)
    assert sms["12"].type == 2 and sms["12"].date == 1790000000123


def test_no_result_is_empty():
    assert parse_content_rows("No result found.\n", ["_id"]) == []
    assert parse_content_rows("", ["_id"]) == []


def test_unexpected_output_and_missing_columns_raise():
    with pytest.raises(AdbError):
        parse_content_rows("Error while accessing provider:sms\n", ["_id"])
    with pytest.raises(AdbError):
        parse_content_rows("Row: 0 _id=1, title=x\n", ["_id", "body"])
    with pytest.raises(AdbError):
        parse_content_rows("Row: 0 title=x, _id=1\n", ["_id", "title"])


def test_contacts_grouped_by_raw_contact_and_deleted_flag():
    raw = [{"_id": "1", "deleted": "0"}, {"_id": "2", "deleted": "1"}, {"_id": "3", "deleted": "0"}]
    data = [
        {"raw_contact_id": "1", "mimetype": ins.MIME_NAME, "data1": "Ana Novak"},
        {"raw_contact_id": "1", "mimetype": ins.MIME_PHONE, "data1": "(415) 555-0123"},
        {"raw_contact_id": "1", "mimetype": ins.MIME_PHONE, "data1": "+1 206 555 0100"},
        {"raw_contact_id": "1", "mimetype": ins.MIME_EMAIL, "data1": "ana@example.com"},
        {"raw_contact_id": "2", "mimetype": ins.MIME_NAME, "data1": "Gone"},
        {"raw_contact_id": "3", "mimetype": "vnd.android.cursor.item/note", "data1": "x"},
        {"raw_contact_id": "3", "mimetype": ins.MIME_PHONE, "data1": None},
    ]
    c = contacts_from_rows(raw, data)
    assert c["1"].name == "Ana Novak"
    assert c["1"].phones == ("(415) 555-0123", "+1 206 555 0100")
    assert c["1"].emails == ("ana@example.com",)
    assert c["2"].deleted and not c["1"].deleted
    assert c["3"].name is None and c["3"].phones == ()


class RecordingAdb:
    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, serial, args, timeout=30.0):
        self.calls.append((serial, args))
        return self.responses.pop(0) if self.responses else b""


def test_query_quotes_arguments_for_device_shell(monkeypatch):
    fake = RecordingAdb([b"Row: 0 _id=5\n"])
    monkeypatch.setattr(ins, "run_adb", fake)
    rows = Inspector("emu-1").query("content://sms", ["_id"], where="address='+1 415'")
    assert rows == [{"_id": "5"}]
    serial, args = fake.calls[0]
    assert serial == "emu-1" and args[0] == "shell"
    assert shlex.split(args[1]) == [
        "content", "query", "--uri", "content://sms", "--projection", "_id", "--where", "address='+1 415'",
    ]


def test_provider_error_text_raises(monkeypatch):
    monkeypatch.setattr(ins, "run_adb", RecordingAdb([b"Error while accessing provider:sms\njava.lang.SecurityException: no\n"]))
    with pytest.raises(AdbError, match="SecurityException"):
        Inspector("emu-1").sms()


def test_insert_sms_binds_typed_values_and_returns_new_id(monkeypatch):
    fake = RecordingAdb([b"", b"Row: 0 _id=42\n"])
    monkeypatch.setattr(ins, "run_adb", fake)
    sid = Inspector("emu-1").insert_sms("+14155550123", "Hi, it's me", 2, 1790000000000)
    assert sid == "42"
    argv = shlex.split(fake.calls[0][1][1])
    assert argv[:4] == ["content", "insert", "--uri", "content://sms"]
    assert "body:s:Hi, it's me" in argv and "type:l:2" in argv and "date:l:1790000000000" in argv


def test_list_files_handles_missing_dir_and_spaces(monkeypatch):
    fake = RecordingAdb([b"/sdcard/m/a b.md\n/sdcard/m/c.md\n"])
    monkeypatch.setattr(ins, "run_adb", fake)
    assert Inspector("e", "/sdcard/m/").list_files("/sdcard/m") == ["/sdcard/m/a b.md", "/sdcard/m/c.md"]
    assert "if [ -d /sdcard/m ]" in fake.calls[0][1][1]


def test_device_time_falls_back_to_seconds(monkeypatch):
    monkeypatch.setattr(ins, "run_adb", RecordingAdb([b"1790000000N\n", b"1790000000\n"]))
    assert Inspector("e").device_time_ms() == 1790000000000


def test_run_adb_raises_on_missing_binary(monkeypatch):
    from harness.device import adb_shell
    monkeypatch.setattr(adb_shell, "ADB", "/nonexistent/adb")
    with pytest.raises(AdbError, match="could not run"):
        ins.run_adb("e", ["devices"])


def _capture_adb(monkeypatch, reply="out"):
    from harness.device import adb_shell
    calls = []

    def fake(serial, *args, timeout=30.0, binary=False):
        calls.append((serial, args, timeout))
        return reply

    monkeypatch.setattr(adb_shell, "adb", fake)
    return calls


def test_adb_shell_quotes_each_argument_as_one_device_word(monkeypatch):
    from harness.device.adb_shell import shell
    calls = _capture_adb(monkeypatch)
    hostile = "a b; rm -rf /sdcard 'x' $(id)"
    assert shell("emulator-5554", "input", "text", hostile) == "out"
    (serial, args, timeout), = calls
    assert serial == "emulator-5554" and args[0] == "shell" and len(args) == 2 and timeout == 30.0
    assert shlex.split(args[1]) == ["input", "text", hostile]


def test_adb_shell_passes_timeout_and_propagates_device_errors(monkeypatch):
    from harness.contracts import DeviceError
    from harness.device import adb_shell
    calls = _capture_adb(monkeypatch)
    adb_shell.shell("s", "true", timeout=7.5)
    assert calls[0][2] == 7.5

    def failing(*a, **kw):
        raise DeviceError("adb shell failed (1): boom")

    monkeypatch.setattr(adb_shell, "adb", failing)
    with pytest.raises(DeviceError, match="boom"):
        adb_shell.shell("s", "false")


def test_private_shell_wrappers_route_through_adb_shell_with_their_defaults(monkeypatch):
    from harness.device.adb import AdbDevice
    from harness.emulator import manager
    from harness.emulator.profiles import business
    from harness.verify.biz_readback import BizReadback
    calls = _capture_adb(monkeypatch)
    manager._shell("getprop", "a b")
    business._shell("pm", "path", "x")
    dev = AdbDevice.__new__(AdbDevice)
    dev.serial = "dev"
    dev._shell("wm", "size")
    rb = BizReadback.__new__(BizReadback)
    rb.serial = "rb"
    rb._shell("ls")
    assert [(s, shlex.split(a[1]), t) for s, a, t in calls] == [
        (manager.SERIAL, ["getprop", "a b"], 30.0),
        (business.SERIAL, ["pm", "path", "x"], 30.0),
        ("dev", ["wm", "size"], 30.0),
        ("rb", ["ls"], 60.0),
    ]
