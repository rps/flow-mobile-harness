"""In-memory stand-in for harness.device.inspect.Inspector."""

from __future__ import annotations

import itertools
import time

from harness.device.inspect import Contact, DeviceState, Event, Sms, file_entry


class FakeInspector:
    def __init__(self, markor_dir: str = "/sdcard/Documents/markor", clock_offset_ms: int = 0,
                 calendar: bool = True) -> None:
        self.markor_dir = markor_dir
        self.clock_offset_ms = clock_offset_ms
        self.calendar = calendar
        self._ids = itertools.count(1)
        self.contacts_db: dict[str, Contact] = {}
        self.sms_db: dict[str, Sms] = {}
        self.events_db: dict[str, Event] = {}
        self.files_db: dict[str, bytes] = {}
        self.calendar_id: str | None = None
        self.shell_calls: list[list[str]] = []

    def shell(self, argv: list[str], timeout: float = 30.0) -> str:
        self.shell_calls.append(list(argv))
        return ""

    def _id(self) -> str:
        return str(next(self._ids))

    # readers
    def contacts(self) -> dict[str, Contact]:
        return dict(self.contacts_db)

    def sms(self, box: str = "all") -> dict[str, Sms]:
        return dict(self.sms_db)

    def calendar_events(self) -> dict[str, Event]:
        return dict(self.events_db)

    def list_files(self, directory: str) -> list[str]:
        return sorted(p for p in self.files_db if p.startswith(directory.rstrip("/") + "/"))

    def pull_file(self, path: str) -> bytes:
        return self.files_db[path]

    def device_time_ms(self) -> int:
        return time.time_ns() // 1_000_000 + self.clock_offset_ms

    def snapshot_state(self) -> DeviceState:
        return DeviceState(
            contacts=self.contacts(),
            sms=self.sms(),
            events=self.calendar_events() if self.calendar else None,
            files={p: file_entry(self.pull_file(p)) for p in self.list_files(self.markor_dir)},
            device_time_offset_ms=self.clock_offset_ms,
        )

    # writers
    def insert_contact(self, name, phone=None, email=None) -> str:
        rid = self._id()
        self.contacts_db[rid] = Contact(rid, name, (phone,) if phone else (), (email,) if email else ())
        return rid

    def ensure_calendar(self) -> str:
        if not self.calendar:
            raise RuntimeError("no calendar provider")
        self.calendar_id = self.calendar_id or self._id()
        return self.calendar_id

    def insert_event(self, calendar_id, title, dtstart, dtend, location=None) -> str:
        eid = self._id()
        self.events_db[eid] = Event(eid, calendar_id, title, dtstart, dtend, location)
        return eid

    def insert_sms(self, address, body, type, date_ms) -> str:
        sid = self._id()
        self.sms_db[sid] = Sms(sid, "1", type, date_ms, address, body)
        return sid

    def push_file(self, path, content: bytes) -> None:
        self.files_db[path] = content

    def delete_file(self, path) -> None:
        self.files_db.pop(path, None)

    def delete_sms(self, sms_id) -> None:
        self.sms_db.pop(sms_id, None)

    def delete_contact(self, raw_id) -> None:
        self.contacts_db.pop(raw_id, None)

    def delete_event(self, event_id) -> None:
        self.events_db.pop(event_id, None)
