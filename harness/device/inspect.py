"""Verifier-only device access over adb. Never imported by harness/agent.

Reads ground truth (contacts, calendar, sms, Markor files) for the verifier
and writes synthetic data for the seeder and verifier self-tests. Parsing of
`content query` output is kept in pure functions so it can be tested without
a device.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.contracts import DeviceError
from harness.device import adb_shell

# Markor's default notebook folder. Unverified until checked on the device;
# every caller can override it.
DEFAULT_MARKOR_DIR = "/sdcard/Documents/markor"

CONTACTS_DATA_URI = "content://com.android.contacts/data"
RAW_CONTACTS_URI = "content://com.android.contacts/raw_contacts"
SMS_URI = "content://sms"
CALENDARS_URI = "content://com.android.calendar/calendars"
EVENTS_URI = "content://com.android.calendar/events"

MIME_NAME = "vnd.android.cursor.item/name"
MIME_PHONE = "vnd.android.cursor.item/phone_v2"
MIME_EMAIL = "vnd.android.cursor.item/email_v2"

# Telephony.TextBasedSmsColumns message types.
SMS_INBOX, SMS_SENT, SMS_DRAFT, SMS_OUTBOX, SMS_FAILED, SMS_QUEUED = 1, 2, 3, 4, 5, 6
SMS_OUTGOING = frozenset({SMS_SENT, SMS_OUTBOX, SMS_QUEUED})
SMS_BOXES = {"all": None, "inbox": SMS_INBOX, "sent": SMS_SENT, "draft": SMS_DRAFT, "outbox": SMS_OUTBOX}

SEED_CALENDAR_ACCOUNT = "labs"

SMS_COLUMNS = ["_id", "thread_id", "type", "date", "address", "body"]
EVENT_COLUMNS = ["_id", "calendar_id", "dtstart", "dtend", "deleted", "eventLocation", "title"]


class AdbError(DeviceError):
    """An adb command failed or a content provider reported an error."""


# --- Records -----------------------------------------------------------------


@dataclass(frozen=True)
class Contact:
    raw_id: str
    name: str | None
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    deleted: bool = False


@dataclass(frozen=True)
class Sms:
    id: str
    thread_id: str | None
    type: int
    date: int
    address: str | None
    body: str | None


@dataclass(frozen=True)
class Event:
    id: str
    calendar_id: str | None
    title: str | None
    dtstart: int | None
    dtend: int | None
    location: str | None
    deleted: bool = False


@dataclass(frozen=True)
class FileEntry:
    sha256: str
    content: bytes = field(repr=False)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class DeviceState:
    """Point-in-time snapshot for diffing. `events` is None when the
    calendar provider could not be read. `device_time_offset_ms` is device
    clock minus host clock."""

    contacts: dict[str, Contact] = field(default_factory=dict)
    sms: dict[str, Sms] = field(default_factory=dict)
    events: dict[str, Event] | None = field(default_factory=dict)
    files: dict[str, FileEntry] = field(default_factory=dict)
    device_time_offset_ms: int = 0


def file_entry(content: bytes) -> FileEntry:
    return FileEntry(hashlib.sha256(content).hexdigest(), content)


# --- Pure parsers ------------------------------------------------------------


def parse_content_rows(text: str, columns: list[str]) -> list[dict[str, str | None]]:
    """Parse `content query --projection a:b:c` output.

    Rows look like `Row: 0 a=1, b=x, y, c=NULL`. Columns are located in
    projection order, so values may contain ", " as long as they do not
    contain the next column's `, name=` marker; put free-text columns last.
    Continuation lines (multi-line values) are joined to the previous row.
    """
    raw_rows: list[str] = []
    for line in text.splitlines():
        if line.startswith("Row: "):
            raw_rows.append(line)
        elif raw_rows:
            raw_rows[-1] += "\n" + line
        elif line.strip() and not line.startswith("No result found"):
            raise AdbError(f"unexpected content output: {line[:200]}")
    rows = []
    for raw in raw_rows:
        _, _, rest = raw.partition(" ")
        _, _, rest = rest.partition(" ")  # drop "Row:" and the row number
        row: dict[str, str | None] = {}
        pos = 0
        for i, col in enumerate(columns):
            prefix = f"{col}="
            if not rest.startswith(prefix, pos):
                raise AdbError(f"column {col!r} not where expected in row: {raw[:200]}")
            start = pos + len(prefix)
            if i + 1 < len(columns):
                end = rest.find(f", {columns[i + 1]}=", start)
                if end < 0:
                    raise AdbError(f"column {columns[i + 1]!r} missing in row: {raw[:200]}")
                value, pos = rest[start:end], end + 2
            else:
                value = rest[start:]
            row[col] = None if value == "NULL" else value
        rows.append(row)
    return rows


def _int(v: str | None, default: int = 0) -> int:
    return default if v is None or v == "" else int(v)


def contacts_from_rows(raw_rows: list[dict], data_rows: list[dict]) -> dict[str, Contact]:
    """Build contacts from raw_contacts (_id, deleted) and data
    (raw_contact_id, mimetype, data1) rows."""
    names: dict[str, str] = {}
    phones: dict[str, list[str]] = {}
    emails: dict[str, list[str]] = {}
    for r in data_rows:
        rid, mime, value = r["raw_contact_id"], r["mimetype"], r["data1"]
        if value is None:
            continue
        if mime == MIME_NAME:
            names[rid] = value
        elif mime == MIME_PHONE:
            phones.setdefault(rid, []).append(value)
        elif mime == MIME_EMAIL:
            emails.setdefault(rid, []).append(value)
    out = {}
    for r in raw_rows:
        rid = r["_id"]
        out[rid] = Contact(
            raw_id=rid,
            name=names.get(rid),
            phones=tuple(sorted(phones.get(rid, []))),
            emails=tuple(sorted(emails.get(rid, []))),
            deleted=_int(r.get("deleted")) != 0,
        )
    return out


def sms_from_rows(rows: list[dict]) -> dict[str, Sms]:
    return {
        r["_id"]: Sms(r["_id"], r["thread_id"], _int(r["type"]), _int(r["date"]), r["address"], r["body"])
        for r in rows
    }


def events_from_rows(rows: list[dict]) -> dict[str, Event]:
    return {
        r["_id"]: Event(
            r["_id"], r["calendar_id"], r["title"],
            _int(r["dtstart"], None), _int(r["dtend"], None),  # type: ignore[arg-type]
            r["eventLocation"], _int(r["deleted"]) != 0,
        )
        for r in rows
    }


def _bind(col: str, value: str | int | None) -> list[str]:
    if value is None:
        return ["--bind", f"{col}:n:"]
    if isinstance(value, bool) or not isinstance(value, int):
        return ["--bind", f"{col}:s:{value}"]
    return ["--bind", f"{col}:l:{value}"]


# --- Seed row layouts (shared by the per-item writers and the batched script) --

SEED_CALENDAR_VALUES: dict[str, Any] = {
    "account_name": SEED_CALENDAR_ACCOUNT, "account_type": "LOCAL",
    "name": SEED_CALENDAR_ACCOUNT, "calendar_displayName": "Personal",
    "calendar_access_level": 700, "ownerAccount": SEED_CALENDAR_ACCOUNT,
    "visible": 1, "sync_events": 1, "calendar_timezone": "UTC",
}
PROVIDER_ERROR_MARKERS = ("Error while accessing provider", "Exception:", "java.lang.")


def _contact_rows(raw_id: Any, name: str, phone: str | None, email: str | None) -> list[dict[str, Any]]:
    rows = [{"raw_contact_id": raw_id, "mimetype": MIME_NAME, "data1": name}]
    if phone:
        rows.append({"raw_contact_id": raw_id, "mimetype": MIME_PHONE, "data1": phone, "data2": 2})
    if email:
        rows.append({"raw_contact_id": raw_id, "mimetype": MIME_EMAIL, "data1": email, "data2": 1})
    return rows


def _event_values(calendar_id: Any, title: str, dtstart: int, dtend: int, location: str | None) -> dict[str, Any]:
    return {"calendar_id": calendar_id, "title": title, "dtstart": dtstart, "dtend": dtend,
            "eventTimezone": "UTC", "eventLocation": location}


def _check_provider_output(out: str, what: str) -> None:
    for err in PROVIDER_ERROR_MARKERS:
        if err in out:
            raise AdbError(f"{what}: {out.strip()[:500]}")


# --- Batched seed script -----------------------------------------------------
#
# One `content insert` costs ~0.9 s on the emulator whatever the transport (it
# spawns an app_process each time), so batching alone does not help: the script
# runs the per-contact chains and the event inserts as parallel subshells. Each
# row carries a unique marker (raw_contacts.sourceid / events.uid2445) so its
# `_id` is looked up exactly instead of by "newest row".

SEED_SCRIPT_PATH = "/data/local/tmp/labs_seed.sh"
SEED_MAX_PARALLEL = 8  # concurrent chains, i.e. concurrent app_process JVMs on the AVD
_FIRST_ID = "| grep -o '_id=[0-9]*' | head -n 1 | sed 's/_id=//'"
_SEED_MARKER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class _Var(str):
    """Name of a shell variable set earlier in the seed script (expanded unquoted)."""


def _bind_sh(col: str, value: Any) -> str:
    """One `--bind` argument, quoted for the device shell. Plan strings are
    always quoted literals; only a _Var is expanded by the shell."""
    if value is None:
        return f"--bind {col}:n:"
    if isinstance(value, _Var):
        return f"--bind {col}:l:${value}"
    if isinstance(value, bool) or not isinstance(value, int):
        return f"--bind {shlex.quote(f'{col}:s:{value}')}"
    return f"--bind {col}:l:{value}"


def _insert_sh(uri: str, values: dict[str, Any]) -> str:
    return " ".join(["content insert --uri", shlex.quote(uri), *(_bind_sh(c, v) for c, v in values.items())])


def _id_sh(uri: str, where: str) -> str:
    return f"content query --uri {shlex.quote(uri)} --projection _id --where {shlex.quote(where)} {_FIRST_ID}"


def seed_script(contacts: list[Any], events: list[Any], marker: str,
                max_parallel: int = SEED_MAX_PARALLEL) -> str:
    """Shell script that inserts contacts (name, phone, email) and events
    (title, start_ms, end_ms, location) and echoes `contact=<n>:<id>` /
    `event=<n>:<id>` lines (n = position in the input). `marker` makes the
    per-row lookup keys unique across runs. Pure: no device access.

    Chains run as background subshells, at most `max_parallel` at a time; each
    one is waited on by pid and a failure is reported as `chain=<name> rc=<rc>`
    (a bare `wait` would discard the statuses)."""
    if not _SEED_MARKER_RE.match(marker):
        raise ValueError(f"bad seed marker {marker!r}")
    if max_parallel < 1:
        raise ValueError("max_parallel must be >= 1")
    lines = ["set -e"]

    def run_chains(chains: list[tuple[str, list[str]]]) -> None:
        for i in range(0, len(chains), max_parallel):
            batch = chains[i:i + max_parallel]
            for name, cmds in batch:
                lines.append("( " + " && ".join(cmds) + " ) &")
                lines.append(f"P_{name}=$!")
            for name, _ in batch:
                lines.append(f'wait $P_{name} || echo "chain={name} rc=$?"')

    contact_chains = []
    for n, c in enumerate(contacts):
        key = f"{marker}-c{n}"
        contact_chains.append((f"c{n}", [
            _insert_sh(RAW_CONTACTS_URI, {"account_type": None, "account_name": None, "sourceid": key}),
            f"RID=$({_id_sh(RAW_CONTACTS_URI, f'sourceid={_sql_str(key)}')})",
            *(_insert_sh(CONTACTS_DATA_URI, row) for row in _contact_rows(_Var("RID"), c.name, c.phone, c.email)),
            f'echo "contact={n}:$RID"',
        ]))
    run_chains(contact_chains)
    if events:
        where = f"account_name={_sql_str(SEED_CALENDAR_ACCOUNT)}"
        lines.append(f"CAL=$({_id_sh(CALENDARS_URI, where)})")
        lines.append('if [ -z "$CAL" ]; then')
        lines.append("  " + _insert_sh(Inspector._calendar_sync_uri(CALENDARS_URI), SEED_CALENDAR_VALUES))
        lines.append(f"  CAL=$({_id_sh(CALENDARS_URI, where)})")
        lines.append("fi")
        event_chains = []
        for n, e in enumerate(events):
            key = f"{marker}-e{n}"
            event_chains.append((f"e{n}", [
                _insert_sh(EVENTS_URI, {**_event_values(_Var("CAL"), e.title, e.start_ms, e.end_ms, e.location),
                                        "uid2445": key}),
                f"EID=$({_id_sh(EVENTS_URI, f'uid2445={_sql_str(key)}')})",
                f'echo "event={n}:$EID"',
            ]))
        run_chains(event_chains)
    return "\n".join(lines) + "\n"


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_seed_failures(out: str) -> list[str]:
    """`chain=<name> rc=<rc>` lines emitted for subshells that exited non-zero."""
    return [m.group(0) for m in re.finditer(r"^chain=[ce]\d+ rc=\d+$", out, re.MULTILINE)]


def parse_seed_output(out: str, n_contacts: int, n_events: int) -> tuple[list[str], list[str]]:
    """Collect `contact=<n>:<id>` / `event=<n>:<id>` lines into position
    order. A missing or non-numeric id leaves "" at that position."""
    contact_ids = [""] * n_contacts
    event_ids = [""] * n_events
    for line in out.splitlines():
        key, _, rest = line.strip().partition("=")
        pos, _, value = rest.partition(":")
        if key not in ("contact", "event") or not pos.isdigit() or not value.isdigit():
            continue
        ids = contact_ids if key == "contact" else event_ids
        if int(pos) < len(ids):
            ids[int(pos)] = value
    return contact_ids, event_ids


# --- adb wrapper -------------------------------------------------------------


def run_adb(serial: str, args: list[str], timeout: float = 30.0) -> bytes:
    """Run `adb -s serial args...` and return stdout bytes. Raises AdbError."""
    try:
        return adb_shell.adb(serial, *args, timeout=timeout, binary=True)  # type: ignore[return-value]
    except DeviceError as e:
        raise AdbError(str(e)[:800]) from e


class Inspector:
    """Harness-side device access for one adb serial."""

    def __init__(self, serial: str, markor_dir: str = DEFAULT_MARKOR_DIR) -> None:
        self.serial = serial
        self.markor_dir = markor_dir.rstrip("/")

    # -- plumbing

    def shell(self, argv: list[str], timeout: float = 30.0) -> str:
        """Run argv on the device shell, each argument quoted."""
        return self.shell_raw(" ".join(shlex.quote(a) for a in argv), timeout)

    def shell_raw(self, command: str, timeout: float = 30.0) -> str:
        return run_adb(self.serial, ["shell", command], timeout).decode("utf-8", errors="replace")

    def _content(self, argv: list[str]) -> str:
        out = self.shell(["content", *argv])
        _check_provider_output(out, f"content {argv[0]} {argv[2] if len(argv) > 2 else ''}")
        return out

    def query(self, uri: str, columns: list[str], where: str | None = None, sort: str | None = None) -> list[dict]:
        argv = ["query", "--uri", uri, "--projection", ":".join(columns)]
        if where:
            argv += ["--where", where]
        if sort:
            argv += ["--sort", sort]
        return parse_content_rows(self._content(argv), columns)

    def _insert(self, uri: str, values: dict[str, str | int | None]) -> None:
        argv = ["insert", "--uri", uri]
        for col, v in values.items():
            argv += _bind(col, v)
        self._content(argv)

    def _last_id(self, uri: str, where: str | None = None) -> str:
        rows = self.query(uri, ["_id"], where=where, sort="_id DESC")
        if not rows:
            raise AdbError(f"insert into {uri} left no row")
        return rows[0]["_id"]

    def _delete(self, uri: str, where: str) -> None:
        self._content(["delete", "--uri", uri, "--where", where])

    # -- readers

    def contacts(self) -> dict[str, Contact]:
        raw = self.query(RAW_CONTACTS_URI, ["_id", "deleted"])
        data = self.query(CONTACTS_DATA_URI, ["raw_contact_id", "mimetype", "data1"])
        return contacts_from_rows(raw, data)

    def sms(self, box: str = "all") -> dict[str, Sms]:
        if box not in SMS_BOXES:
            raise ValueError(f"unknown sms box {box!r}")
        uri = SMS_URI if box == "all" else f"{SMS_URI}/{box}"
        return sms_from_rows(self.query(uri, SMS_COLUMNS))

    def calendar_events(self) -> dict[str, Event]:
        return events_from_rows(self.query(EVENTS_URI, EVENT_COLUMNS))

    def list_files(self, directory: str) -> list[str]:
        d = shlex.quote(directory)
        out = self.shell_raw(f"if [ -d {d} ]; then find {d} -type f; fi")
        return sorted(line for line in out.splitlines() if line)

    def pull_file(self, path: str) -> bytes:
        return run_adb(self.serial, ["exec-out", "cat", path])

    def device_time_ms(self) -> int:
        out = self.shell(["date", "+%s%3N"]).strip()
        if out.isdigit() and len(out) >= 13:
            return int(out)
        return int(self.shell(["date", "+%s"]).strip()) * 1000

    def snapshot_state(self) -> DeviceState:
        host_before = time.time_ns() // 1_000_000
        device_now = self.device_time_ms()
        host_after = time.time_ns() // 1_000_000
        try:
            events: dict[str, Event] | None = self.calendar_events()
        except AdbError:
            events = None
        return DeviceState(
            contacts=self.contacts(),
            sms=self.sms(),
            events=events,
            files={p: file_entry(self.pull_file(p)) for p in self.list_files(self.markor_dir)},
            device_time_offset_ms=device_now - (host_before + host_after) // 2,
        )

    # -- writers (seeding and self-test gold/decoy injection only)

    def insert_contact(self, name: str, phone: str | None = None, email: str | None = None) -> str:
        self._insert(RAW_CONTACTS_URI, {"account_type": None, "account_name": None})
        rid = self._last_id(RAW_CONTACTS_URI)
        for row in _contact_rows(int(rid), name, phone, email):
            self._insert(CONTACTS_DATA_URI, row)
        return rid

    def ensure_calendar(self) -> str:
        where = f"account_name='{SEED_CALENDAR_ACCOUNT}'"
        rows = self.query(CALENDARS_URI, ["_id"], where=where)
        if rows:
            return rows[0]["_id"]
        self._insert(self._calendar_sync_uri(CALENDARS_URI), SEED_CALENDAR_VALUES)
        return self._last_id(CALENDARS_URI, where=where)

    def insert_event(self, calendar_id: str, title: str, dtstart: int, dtend: int, location: str | None = None) -> str:
        self._insert(EVENTS_URI, _event_values(int(calendar_id), title, dtstart, dtend, location))
        return self._last_id(EVENTS_URI)

    def insert_sms(self, address: str, body: str, type: int, date_ms: int) -> str:
        self._insert(SMS_URI, {"address": address, "body": body, "type": type, "date": date_ms, "read": 1})
        return self._last_id(SMS_URI)

    def apply_batch(self, contacts: list[Any], events: list[Any], notes: list[tuple[str, bytes]]
                    ) -> tuple[list[str], list[str]]:
        """Seed contacts and events with one pushed shell script run in a single
        `adb shell`, then push each note file. Returns (contact_ids, event_ids).

        Raises AdbError if the provider reports an error or an id is missing.
        """
        marker = f"labs-seed-{time.time_ns():x}"
        script = seed_script(contacts, events, marker)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
        try:
            run_adb(self.serial, ["push", f.name, SEED_SCRIPT_PATH])
        finally:
            Path(f.name).unlink(missing_ok=True)
        try:
            out = self.shell(["sh", SEED_SCRIPT_PATH], timeout=120.0)
        finally:
            try:
                self.shell(["rm", "-f", SEED_SCRIPT_PATH])
            except AdbError:
                pass
        _check_provider_output(out, "seed script")
        failures = parse_seed_failures(out)
        if failures:
            raise AdbError(f"seed script chains failed ({', '.join(failures)}): {out.strip()[-300:]}")
        contact_ids, event_ids = parse_seed_output(out, len(contacts), len(events))
        if "" in contact_ids or "" in event_ids:
            raise AdbError(
                f"seed script returned {len(contact_ids) - contact_ids.count('')}/{len(contacts)} contact ids and "
                f"{len(event_ids) - event_ids.count('')}/{len(events)} event ids: {out.strip()[-300:]}"
            )
        for path, content in notes:
            self.push_file(path, content)
        return contact_ids, event_ids

    def push_file(self, path: str, content: bytes) -> None:
        self.shell(["mkdir", "-p", str(Path(path).parent)])
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
        try:
            run_adb(self.serial, ["push", f.name, path])
        finally:
            Path(f.name).unlink(missing_ok=True)

    def delete_file(self, path: str) -> None:
        self.shell(["rm", "-f", path])

    def delete_sms(self, sms_id: str) -> None:
        self._delete(SMS_URI, f"_id={int(sms_id)}")

    def delete_contact(self, raw_id: str) -> None:
        self._delete(f"{RAW_CONTACTS_URI}?caller_is_syncadapter=true", f"_id={int(raw_id)}")

    def delete_event(self, event_id: str) -> None:
        self._delete(self._calendar_sync_uri(EVENTS_URI), f"_id={int(event_id)}")

    @staticmethod
    def _calendar_sync_uri(uri: str) -> str:
        return f"{uri}?caller_is_syncadapter=true&account_name={SEED_CALENDAR_ACCOUNT}&account_type=LOCAL"
