"""Agent-facing Device over adb. Never import harness.device.inspect from here.

Coordinates the agent gives (tap, swipe) and the bounds it reads in ui_tree
are in screenshot (scaled) space; they are mapped to real pixels here.
"""

from __future__ import annotations

import io
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from harness.contracts import Config, DeviceError, QueryNotAllowed, Screenshot
from harness.device.adb_shell import adb, shell

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
DUMP_PATH = "/sdcard/window_dump.xml"
TEXT_CHUNK = 100
MAX_LABEL = 120
# Text of editable fields (a note open in an editor) is shown longer; everything else stays at MAX_LABEL.
MAX_EDIT_TEXT_LABEL = 600
# Text of password fields is replaced by this before anything leaves ui_tree().
REDACTED = "[redacted]"

# name -> (uri, projection, where, sort). Built only from these constants, so
# every query is a read-only `content query` with no agent-controlled text.
QUERIES: dict[str, tuple[str, list[str], str | None, str | None]] = {
    "contacts.list": (
        "content://com.android.contacts/data",
        ["contact_id", "display_name", "mimetype", "data1"],
        None,
        "contact_id ASC",
    ),
    "calendar.events": (
        "content://com.android.calendar/events",
        ["_id", "title", "dtstart", "dtend", "allDay", "eventLocation", "description"],
        "deleted=0",
        "dtstart ASC",
    ),
    "sms.list": (
        "content://sms",
        ["_id", "address", "body", "date", "type", "read"],
        None,
        "date DESC",
    ),
    "snackorders.orders": (
        "content://com.labs.snackorders.provider/orders",
        ["_id", "placed_at", "status", "item_count", "subtotal_cents", "shipping_cents", "total_cents"],
        None,
        None,
    ),
}
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Settling after an action. On p2_harness_api36 a cold app start leaves
# mCurrentFocus=null for about 5 s while the old dump still shows the launcher.
SETTLE_MIN_WAIT_S = 0.3  # let a transition begin before the first focus read
SETTLE_POLL_S = 0.15
SETTLE_MAX_S = 8.0  # a cold Jetsnack start takes focus 5.7-6.1 s after the tap
FOCUS_RE = re.compile(r"mCurrentFocus=(.*)")
WINDOW_PACKAGE_RE = re.compile(r"\bu\d+ ([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)[/}]")


@dataclass(frozen=True)
class SettleResult:
    seconds: float
    reason: str  # "stable", "focus_only" or "cap"
    tree: str | None = None  # the stable dump; None for focus_only and cap
    focus: str | None = None  # the focused window settling ended on; on cap the last read (None if null)


def window_package(focus: str) -> str | None:
    """Package of a focused-window description such as
    `Window{594f243 u0 com.labs.snackorders/com.example.MainActivity}`; None
    for windows named without one (NotificationShade, PopupWindow:...)."""
    m = WINDOW_PACKAGE_RE.search(focus)
    return m.group(1) if m else None


def tree_packages(tree: str) -> list[str]:
    """Packages listed in the header line of a ui_tree() dump. Empty when no
    node survived trimming (image-only, canvas or WebView screens)."""
    header = tree.split("\n", 1)[0]
    _, _, tail = header.partition("packages: ")
    return [p.strip() for p in tail.split(",") if p.strip() and p.strip() != "-"]


def wait_for_settle(
    focus: Callable[[], str | None],
    dump: Callable[[], str],
    *,
    max_s: float = SETTLE_MAX_S,
    min_wait_s: float = SETTLE_MIN_WAIT_S,
    poll_s: float = SETTLE_POLL_S,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    before: str | None = None,
) -> SettleResult:
    """Wait until the UI is settled or `max_s` has passed.

    Focus gate (always): two consecutive focus reads give the same non-None
    window. `focus` returns None while no window has focus (an activity
    transition). If that window is still `before` (the window focused before
    the action) and every read gave it, the wait ends there without a dump
    ("focus_only"). Otherwise (the window changed, or `before` is unknown) two
    consecutive dumps, each taken right after a focus read that still gives
    that window, must be identical and list its package ("stable"). The
    package check is skipped when the window is named without a package or
    the dump lists none. A dump already in flight may overrun `max_s`.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    t0 = clock()

    def elapsed() -> float:
        return clock() - t0

    sleep(max(0.0, min(min_wait_s, max_s)))
    last_focus: str | None = None
    last_tree: str | None = None
    changed = before is None
    while True:
        current = focus()
        if current != before:
            changed = True
        if current is None or current != last_focus:
            last_focus, last_tree = current, None
            if elapsed() >= max_s:
                return SettleResult(round(elapsed(), 3), "cap", None, last_focus)
            sleep(max(0.0, min(poll_s, max_s - elapsed())))
            continue
        if not changed:
            return SettleResult(round(elapsed(), 3), "focus_only", None, current)
        if elapsed() >= max_s:
            return SettleResult(round(elapsed(), 3), "cap", None, last_focus)
        tree = dump()
        package = window_package(current)
        packages = tree_packages(tree)
        if package is not None and packages and package not in packages:
            last_tree = None  # the dump still shows the previous window
        elif tree == last_tree:
            return SettleResult(round(elapsed(), 3), "stable", tree, current)
        else:
            last_tree = tree


def scale_factor(width: int, height: int, max_px: int) -> float:
    """Downscale factor so the longest side is at most max_px (never upscales)."""
    return min(1.0, max_px / max(width, height))


def parse_content_rows(out: str, keys: list[str]) -> list[dict[str, Any]]:
    """Parse `content query` output into dicts.

    Lines look like `Row: 0 a=1, b=x, y`; values can contain ", " and newlines,
    so each value runs up to the next expected `, key=` marker.
    """
    rows: list[str] = []
    for line in out.splitlines():
        if line.startswith("Row: "):
            rows.append(line)
        elif rows:
            rows[-1] += "\n" + line
    result = []
    for raw in rows:
        body = raw.split(" ", 2)[2] if raw.count(" ") >= 2 else ""
        row: dict[str, Any] = {}
        pos = 0
        for i, key in enumerate(keys):
            start = body.find(f"{key}=", pos)
            if start < 0:
                row[key] = None
                continue
            start += len(key) + 1
            end = body.find(f", {keys[i + 1]}=", start) if i + 1 < len(keys) else len(body)
            if end < 0:
                end = len(body)
            value = body[start:end]
            row[key] = None if value == "NULL" else value
            pos = end
        result.append(row)
    return result


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return None


class AdbDevice:
    """Device protocol implementation over adb for one emulator serial."""

    def __init__(self, serial: str, config: Config | None = None, max_tree_lines: int = 150) -> None:
        self.serial = serial
        self.config = config or Config()
        self.max_tree_lines = max_tree_lines
        self._size: tuple[int, int] | None = None
        self._focus: str | None = None  # focused window the last settle() ended on

    # --- helpers -------------------------------------------------------------

    def _shell(self, *args: str, timeout: float = 30.0) -> str:
        return shell(self.serial, *args, timeout=timeout)

    def _real_size(self) -> tuple[int, int]:
        if self._size is None:
            out = self._shell("wm", "size")
            m = re.search(r"Override size: (\d+)x(\d+)", out) or re.search(r"Physical size: (\d+)x(\d+)", out)
            if not m:
                raise DeviceError(f"cannot read screen size: {out.strip()}")
            self._size = (int(m.group(1)), int(m.group(2)))
        return self._size

    def _factor(self) -> float:
        return scale_factor(*self._real_size(), self.config.screenshot_max_px)

    def _to_device(self, x: int, y: int) -> tuple[int, int]:
        f = self._factor()
        return round(x / f), round(y / f)

    def _to_scaled(self, x: int, y: int) -> tuple[int, int]:
        f = self._factor()
        return round(x * f), round(y * f)

    # --- Device protocol -----------------------------------------------------

    def screenshot(self) -> Screenshot:
        raw = adb(self.serial, "exec-out", "screencap", "-p", binary=True, timeout=30)
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            raise DeviceError(f"screencap returned no image: {exc}") from exc
        w, h = img.size
        f = scale_factor(w, h, self.config.screenshot_max_px)
        sw, sh = max(1, round(w * f)), max(1, round(h * f))
        if (sw, sh) != (w, h):
            img = img.resize((sw, sh), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return Screenshot(png=buf.getvalue(), width=w, height=h, scaled_width=sw, scaled_height=sh)

    def ui_tree(self) -> str:
        xml = ""
        for attempt in range(3):
            try:
                self._shell("uiautomator", "dump", DUMP_PATH, timeout=30)
                xml = adb(self.serial, "exec-out", "cat", DUMP_PATH, timeout=15)
                break
            except DeviceError:
                if attempt == 2:
                    raise
                time.sleep(1)
        try:
            root = ET.fromstring(xml[xml.find("<"):])
        except ET.ParseError as exc:
            raise DeviceError(f"uiautomator dump unparsable: {exc}") from exc
        return self._format_tree(root)

    def _format_tree(self, root: ET.Element) -> str:
        sw, sh = self._to_scaled(*self._real_size())
        lines: list[str] = []
        packages: list[str] = []
        for node in root.iter("node"):
            a = node.attrib
            if a.get("visible-to-user") == "false":
                continue
            m = BOUNDS_RE.fullmatch(a.get("bounds", ""))
            if not m:
                continue
            x1, y1 = self._to_scaled(int(m.group(1)), int(m.group(2)))
            x2, y2 = self._to_scaled(int(m.group(3)), int(m.group(4)))
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(sw, x2), min(sh, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            text, desc = a.get("text", ""), a.get("content-desc", "")
            cls = a.get("class", "").rsplit(".", 1)[-1]
            flags = [f for f in ("clickable", "long-clickable", "scrollable", "checkable", "focused") if a.get(f) == "true"]
            if not (text or desc or flags or cls == "EditText"):
                continue
            if a.get("checkable") == "true":
                flags.append("checked" if a.get("checked") == "true" else "unchecked")
            if a.get("selected") == "true":
                flags.append("selected")
            if a.get("enabled") == "false":
                flags.append("disabled")
            if a.get("password") == "true":
                flags.append("password")
                # Both free-text attributes we print may echo the typed secret.
                text = REDACTED if text else text
                desc = REDACTED if desc else desc
            pkg = a.get("package", "")
            if pkg and pkg not in packages:
                packages.append(pkg)
            parts = [cls or "View"]
            if text:
                parts.append(f'"{_label(text, MAX_EDIT_TEXT_LABEL if cls == "EditText" else MAX_LABEL)}"')
            if desc:
                parts.append(f'desc="{_label(desc)}"')
            rid = a.get("resource-id", "").split(":id/", 1)[-1]
            if rid:
                parts.append(f"id={rid}")
            parts.append(f"center=({(x1 + x2) // 2},{(y1 + y2) // 2})")
            parts.append(f"bounds=({x1},{y1},{x2},{y2})")
            parts.extend(flags)
            lines.append(" ".join(parts))
        header = f"screen {sw}x{sh} (screenshot coordinates); packages: {', '.join(packages) or '-'}"
        dropped = max(0, len(lines) - self.max_tree_lines)
        lines = lines[: self.max_tree_lines]
        if dropped:
            lines.append(f"... {dropped} more nodes dropped")
        return "\n".join([header, *lines])

    def focused_window(self) -> str | None:
        """mCurrentFocus of the first display, or None while it is null or a
        splash (starting) window, i.e. during an activity transition."""
        out = adb(self.serial, "shell", "dumpsys window | grep mCurrentFocus= || true", timeout=15)
        m = FOCUS_RE.search(out)
        value = m.group(1).strip() if m else "null"
        return None if value == "null" or "Splash Screen" in value else value

    def settle(self, max_s: float = SETTLE_MAX_S) -> SettleResult:
        """Wait for the UI to settle after an action (see wait_for_settle),
        comparing with the window the previous settle ended on (unknown before
        the first settle or after a cap with no window focused)."""
        before, self._focus = self._focus, None
        result = wait_for_settle(self.focused_window, self.ui_tree, max_s=max_s, before=before)
        self._focus = result.focus
        return result

    def tap(self, x: int, y: int) -> None:
        dx, dy = self._to_device(x, y)
        self._shell("input", "tap", str(dx), str(dy))

    def type_text(self, text: str) -> None:
        if any(ord(c) > 126 or (ord(c) < 32 and c != "\n") for c in text):
            raise DeviceError("type_text supports printable ASCII and newlines only")
        for i, line in enumerate(text.split("\n")):
            if i:
                self._shell("input", "keyevent", "KEYCODE_ENTER")
            for start in range(0, len(line), TEXT_CHUNK):
                chunk = line[start : start + TEXT_CHUNK]
                # `input text` treats %s as a space; the device shell gets the
                # argument single-quoted via _shell, so other chars are literal.
                self._shell("input", "text", chunk.replace(" ", "%s"))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        a, b = self._to_device(x1, y1)
        c, d = self._to_device(x2, y2)
        self._shell("input", "swipe", str(a), str(b), str(c), str(d), str(max(1, int(duration_ms))))

    def back(self) -> None:
        self._shell("input", "keyevent", "KEYCODE_BACK")

    def home(self) -> None:
        self._shell("input", "keyevent", "KEYCODE_HOME")

    def open_app(self, package: str) -> None:
        if not PACKAGE_RE.fullmatch(package):
            raise DeviceError(f"not a package name: {package!r}")
        installed = {l.removeprefix("package:").strip() for l in self._shell("pm", "list", "packages").splitlines()}
        if package not in installed:
            raise DeviceError(f"package not installed: {package}")
        out = self._shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        if "Events injected: 1" not in out:
            raise DeviceError(f"could not launch {package}: {out.strip()[-200:]}")

    def allowed_queries(self) -> list[str]:
        return list(QUERIES)

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name not in QUERIES:
            raise QueryNotAllowed(f"query not allowed: {name!r}; allowed: {', '.join(QUERIES)}")
        params = dict(params or {})
        limit = params.pop("limit", DEFAULT_LIMIT)
        if params:
            raise DeviceError(f"unknown parameters: {', '.join(params)}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise DeviceError(f"limit must be an integer 1..{MAX_LIMIT}")
        uri, keys, where, sort = QUERIES[name]
        args = ["content", "query", "--uri", uri, "--projection", ":".join(keys)]
        if where:
            args += ["--where", where]
        if sort:
            args += ["--sort", sort]
        out = self._shell(*args)
        if "Error while accessing provider" in out or out.startswith("Error"):
            raise DeviceError(f"{name}: {out.strip()[:200]}")
        rows = parse_content_rows(out, keys)
        if name == "contacts.list":
            rows = _group_contacts(rows)
        elif name == "calendar.events":
            for r in rows:
                r["start_utc"], r["end_utc"] = _iso(r.get("dtstart")), _iso(r.get("dtend"))
        elif name == "sms.list":
            for r in rows:
                r["date_utc"] = _iso(r.get("date"))
                r["box"] = {"1": "inbox", "2": "sent", "3": "draft"}.get(r.get("type") or "", r.get("type"))
        return {"rows": rows[:limit], "total": len(rows)}


def _label(s: str, limit: int = MAX_LABEL) -> str:
    s = s.replace("\n", "\\n").replace('"', "'")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _group_contacts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contacts: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = contacts.setdefault(r["contact_id"] or "", {"id": r["contact_id"], "name": r["display_name"], "phones": [], "emails": []})
        mime, value = r.get("mimetype") or "", r.get("data1")
        if value and mime.endswith("/phone_v2"):
            c["phones"].append(value)
        elif value and mime.endswith("/email_v2"):
            c["emails"].append(value)
    return list(contacts.values())
