"""Stand-ins for `--fake` runs: no emulator, no adb, no API calls.

FakeEmulator.restore_snapshot resets a MemoryInspector; ScriptedClient
answers the Messages API with a fixed tool sequence ending in finish(done).
The fake device changes no state, so a task's verifier is expected to FAIL:
a fake run exercises the sequence, not the agent.
"""

from __future__ import annotations

import itertools
import struct
import time
import zlib
from typing import Any

from anthropic.types import Message, ToolUseBlock, Usage

from harness.contracts import DeviceError, QueryNotAllowed, Screenshot
from harness.device.inspect import Contact, DeviceState, Event, Sms, file_entry

FAKE_MODEL = "fake-model"
FAKE_SERIAL = "fake"
MARKOR = "net.gsantner.markor"


class MemoryInspector:
    """In-memory Inspector: the readers and seed writers the runner uses."""

    def __init__(self, markor_dir: str = "/sdcard/Documents/markor") -> None:
        self.markor_dir = markor_dir
        self.reset()

    def reset(self) -> None:
        self._ids = itertools.count(1)
        self.contacts_db: dict[str, Contact] = {}
        self.sms_db: dict[str, Sms] = {}
        self.events_db: dict[str, Event] = {}
        self.files_db: dict[str, bytes] = {}
        self.calendar_id: str | None = None

    def _id(self) -> str:
        return str(next(self._ids))

    def snapshot_state(self) -> DeviceState:
        prefix = self.markor_dir.rstrip("/") + "/"
        return DeviceState(
            contacts=dict(self.contacts_db),
            sms=dict(self.sms_db),
            events=dict(self.events_db),
            files={p: file_entry(c) for p, c in sorted(self.files_db.items()) if p.startswith(prefix)},
        )

    def device_time_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def insert_contact(self, name: str, phone: str | None = None, email: str | None = None) -> str:
        rid = self._id()
        self.contacts_db[rid] = Contact(rid, name, (phone,) if phone else (), (email,) if email else ())
        return rid

    def ensure_calendar(self) -> str:
        self.calendar_id = self.calendar_id or self._id()
        return self.calendar_id

    def insert_event(self, calendar_id: str, title: str, dtstart: int, dtend: int, location: str | None = None) -> str:
        eid = self._id()
        self.events_db[eid] = Event(eid, calendar_id, title, dtstart, dtend, location)
        return eid

    def insert_sms(self, address: str, body: str, type: int, date_ms: int) -> str:
        sid = self._id()
        self.sms_db[sid] = Sms(sid, "1", type, date_ms, address, body)
        return sid

    def push_file(self, path: str, content: bytes) -> None:
        self.files_db[path] = content


class FakeEmulator:
    """restore_snapshot clears the inspector, like a snapshot restore would."""

    SERIAL = FAKE_SERIAL

    def __init__(self, inspector: MemoryInspector) -> None:
        self.inspector = inspector
        self.restores: list[str] = []

    def restore_snapshot(self, name: str) -> float:
        self.restores.append(name)
        self.inspector.reset()
        return 0.0


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class _FallbackDevice:
    """Used when harness.device.fake is not available: a grey screen, no queries."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, tuple]] = []
        self._png = _png(54, 120)

    def screenshot(self) -> Screenshot:
        return Screenshot(self._png, 1080, 2400, 54, 120)

    def ui_tree(self) -> str:
        return "screen (fake)"

    def tap(self, x: int, y: int) -> None:
        self.actions.append(("tap", (x, y)))

    def type_text(self, text: str) -> None:
        self.actions.append(("type_text", (text,)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.actions.append(("swipe", (x1, y1, x2, y2, duration_ms)))

    def back(self) -> None:
        self.actions.append(("back", ()))

    def home(self) -> None:
        self.actions.append(("home", ()))

    def open_app(self, package: str) -> None:
        if package != MARKOR:
            raise DeviceError(f"package not installed: {package}")
        self.actions.append(("open_app", (package,)))

    def allowed_queries(self) -> list[str]:
        return []

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        raise QueryNotAllowed(f"query not allowed: {name!r}")


def make_fake_device() -> Any:
    try:
        from harness.device.fake import FakeDevice
    except ImportError:
        return _FallbackDevice()
    return FakeDevice()


SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("open_app", {"package": MARKOR}),
    ("tap", {"x": 27, "y": 60}),
    ("type_text", {"text": "fake run"}),
    ("back", {}),
    ("finish", {"verdict": "done", "summary": "scripted fake run; the fake device changes nothing"}),
]


class ScriptedClient:
    """Stands in for anthropic.Anthropic. Each create() returns the next
    scripted tool call; after the script, it keeps calling finish."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        self.script = list(script or SCRIPT)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        name, tool_input = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return Message(
            id=f"msg_fake_{self.calls}",
            type="message",
            role="assistant",
            model=FAKE_MODEL,
            content=[ToolUseBlock(type="tool_use", id=f"toolu_fake_{self.calls}", name=name, input=dict(tool_input))],
            stop_reason="tool_use",
            stop_sequence=None,
            usage=Usage(input_tokens=0, output_tokens=0),
        )
