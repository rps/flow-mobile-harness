"""ui_tree() formatting from a fake uiautomator dump: password redaction."""

import pytest

from harness.device import adb as adb_mod
from harness.contracts import DeviceError
from harness.device.adb import REDACTED, AdbDevice

SECRET = "hunter2-top-secret"

DUMP = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="Username" resource-id="com.example:id/user" class="android.widget.EditText"
        package="com.example" content-desc="" checkable="false" checked="false" clickable="true"
        enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false"
        password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,100]" />
  <node index="1" text="{SECRET}" resource-id="com.example:id/pass" class="android.widget.EditText"
        package="com.example" content-desc="{SECRET}" checkable="false" checked="false" clickable="true"
        enabled="true" focusable="true" focused="true" scrollable="false" long-clickable="false"
        password="true" selected="false" visible-to-user="true" bounds="[0,100][1080,200]" />
  <node index="2" text="" resource-id="com.example:id/empty_pass" class="android.widget.EditText"
        package="com.example" content-desc="" checkable="false" checked="false" clickable="true"
        enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false"
        password="true" selected="false" visible-to-user="true" bounds="[0,200][1080,300]" />
</hierarchy>
"""


class FakeDevice(AdbDevice):
    def __init__(self, dump: str, dump_failures: int = 0) -> None:
        super().__init__("fake")
        self._size = (1080, 2400)
        self.dump = dump
        self.dump_failures = dump_failures
        self.shell_calls: list[tuple[str, ...]] = []

    def _shell(self, *args: str, timeout: float = 30.0) -> str:
        self.shell_calls.append(args)
        if args[:2] == ("uiautomator", "dump") and self.dump_failures:
            self.dump_failures -= 1
            raise DeviceError("uiautomator: could not get idle state")
        return ""


def _wire(monkeypatch, dev):
    monkeypatch.setattr(adb_mod, "adb", lambda serial, *args, **kw: dev.dump)
    monkeypatch.setattr(adb_mod.time, "sleep", lambda s: None)
    return dev


@pytest.fixture
def device(monkeypatch):
    return _wire(monkeypatch, FakeDevice(DUMP))


def test_password_text_and_desc_are_redacted_and_flagged(device):
    tree = device.ui_tree()
    assert SECRET not in tree
    pass_line = next(l for l in tree.splitlines() if "id=pass " in l)
    assert f'"{REDACTED}" desc="{REDACTED}"' in pass_line
    assert pass_line.endswith("clickable focused password")


def test_non_password_text_is_kept(device):
    tree = device.ui_tree()
    user_line = next(l for l in tree.splitlines() if "id=user " in l)
    assert '"Username"' in user_line
    assert "password" not in user_line


def test_empty_password_field_shows_flag_without_fake_text(device):
    tree = device.ui_tree()
    line = next(l for l in tree.splitlines() if "id=empty_pass " in l)
    assert REDACTED not in line
    assert line.endswith("clickable password")


def test_raw_dump_never_returned(device):
    tree = device.ui_tree()
    assert "<node" not in tree
    assert tree.startswith("screen ")


def test_unparsable_dump_raises(monkeypatch):
    dev = _wire(monkeypatch, FakeDevice("garbage"))
    with pytest.raises(DeviceError, match="unparsable"):
        dev.ui_tree()


def test_dump_is_retried_and_result_still_redacted(monkeypatch):
    dev = _wire(monkeypatch, FakeDevice(DUMP, dump_failures=2))
    tree = dev.ui_tree()
    assert SECRET not in tree and REDACTED in tree
    assert sum(c[:2] == ("uiautomator", "dump") for c in dev.shell_calls) == 3


def test_three_dump_failures_raise(monkeypatch):
    dev = _wire(monkeypatch, FakeDevice(DUMP, dump_failures=3))
    with pytest.raises(DeviceError, match="idle state"):
        dev.ui_tree()


def test_snackorders_query_is_a_fixed_read_only_entry():
    uri, cols, where, sort = adb_mod.QUERIES["snackorders.orders"]
    assert uri == "content://com.labs.snackorders.provider/orders"
    assert cols == ["_id", "placed_at", "status", "item_count", "subtotal_cents", "shipping_cents", "total_cents"]
    assert where is None and sort is None
    assert "snackorders.orders" in AdbDevice("fake").allowed_queries()


def _node(index, cls, text, desc="", password="false", top=0):
    return (f'<node index="{index}" text="{text}" resource-id="com.example:id/n{index}" class="android.widget.{cls}" '
            f'package="com.example" content-desc="{desc}" checkable="false" checked="false" clickable="true" '
            f'enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" '
            f'password="{password}" selected="false" visible-to-user="true" bounds="[0,{top}][1080,{top + 100}]" />')


def _label_of(tree, index):
    line = next(l for l in tree.splitlines() if f"id=n{index} " in l)
    return line.split('"')[1]


def test_edit_text_shows_up_to_600_characters_and_other_nodes_stay_at_120(monkeypatch):
    note, long_note, long_view = "n" * 590, "e" * 700, "v" * 300
    nodes = [_node(0, "EditText", note, desc="d" * 300, top=0), _node(1, "EditText", long_note, top=100),
             _node(2, "TextView", long_view, top=200)]
    dump = f"<?xml version='1.0' encoding='UTF-8' ?><hierarchy rotation=\"0\">{''.join(nodes)}</hierarchy>"
    tree = _wire(monkeypatch, FakeDevice(dump)).ui_tree()
    assert _label_of(tree, 0) == note  # a long note in an editor arrives whole
    assert _label_of(tree, 1) == "e" * 599 + "…"
    assert _label_of(tree, 2) == "v" * 119 + "…"  # non-editable text unchanged
    line0 = next(l for l in tree.splitlines() if "id=n0 " in l)
    assert f'desc="{"d" * 119}…"' in line0  # descriptions keep the short cap, editable or not


def test_long_password_edit_text_is_still_redacted(monkeypatch):
    secret = "s3cret-" * 60  # 420 characters, inside the editable-text cap
    dump = ("<?xml version='1.0' encoding='UTF-8' ?><hierarchy rotation=\"0\">"
            f"{_node(0, 'EditText', secret, desc=secret, password='true')}</hierarchy>")
    tree = _wire(monkeypatch, FakeDevice(dump)).ui_tree()
    assert "s3cret" not in tree
    assert f'"{REDACTED}" desc="{REDACTED}"' in tree and "password" in tree
