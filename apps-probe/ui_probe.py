"""Minimal adb UI driver used while provisioning the business AVD by hand.

    python apps-probe/ui_probe.py [-s SERIAL] shot OUT.png
    python apps-probe/ui_probe.py dump            # trimmed node list (text, desc, id, bounds, focus, password)
    python apps-probe/ui_probe.py tap X Y | swipe X1 Y1 X2 Y2 | back | home | key KEYCODE
    python apps-probe/ui_probe.py type TEXT       # plain text
    python apps-probe/ui_probe.py type-key KEY    # types the value of KEY from .env (allow-listed keys only),
                                                  # prints only its length; with --password it refuses unless the
                                                  # focused node is a password field, without it refuses if it is
    python apps-probe/ui_probe.py start PKG/ACT | pm PKG

Dumps are read in memory and never written to disk, so a login page's
field contents are not left behind. Node text longer than 60 chars is cut.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
ADB = str(SDK / "platform-tools" / "adb")
SERIAL = os.environ.get("PROBE_SERIAL", "emulator-5592")


def adb(*args: str, timeout: float = 60, binary: bool = False) -> str | bytes:
    proc = subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        sys.exit(f"adb {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def shell(*args: str, timeout: float = 60) -> str:
    return adb("shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)


def dump_xml() -> ET.Element:
    out = adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=90)
    start = out.find("<?xml")
    if start < 0:
        start = out.find("<hierarchy")
    end = out.rfind("</hierarchy>")
    if start < 0 or end < 0:
        sys.exit(f"no hierarchy in dump: {out[:200]!r}")
    return ET.fromstring(out[start:end + len("</hierarchy>")])


def nodes(root: ET.Element) -> list[dict]:
    out = []
    for n in root.iter("node"):
        a = n.attrib
        if not (a.get("text") or a.get("content-desc") or a.get("resource-id") or a.get("focused") == "true"
                or a.get("clickable") == "true" or a.get("password") == "true"):
            continue
        out.append(a)
    return out


def fmt(a: dict, redact: bool) -> str:
    text = a.get("text", "")
    if a.get("password") == "true" or redact:
        text = "[redacted]" if text else ""
    text = text[:60]
    flags = "".join(f for f, k in (("F", "focused"), ("C", "clickable"), ("P", "password"), ("K", "checked")) if a.get(k) == "true")
    rid = a.get("resource-id", "").split("/")[-1]
    return f"{a.get('bounds')} {a.get('class', '').split('.')[-1]:<14} {flags:<4} id={rid!r} text={text!r} desc={a.get('content-desc', '')[:60]!r}"


def center(bounds: str) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", bounds))
    return (x1 + x2) // 2, (y1 + y2) // 2


def focused_is_password(root: ET.Element) -> bool:
    foc = [n.attrib for n in root.iter("node") if n.attrib.get("focused") == "true"]
    return any(a.get("password") == "true" for a in foc) and all(a.get("password") == "true" or a.get("class") != "android.widget.EditText" for a in foc)


ENV_FILE = Path(os.environ.get("PROBE_ENV_FILE", Path(os.environ.get("LABS_REPO_ROOT", Path(__file__).resolve().parents[1])) / ".env"))
ALLOWED_KEYS = {"TIMECAMP_EMAIL", "TIMECAMP_PASSWORD", "INSIGHTLY_EMAIL", "INSIGHTLY_PASSWORD",
                "INVOICE_NINJA_EMAIL", "INVOICE_NINJA_PASSWORD", "GOOGLE_USERNAME", "GOOGLE_PASSWORD"}


def env_value(key: str) -> str | None:
    """Read one allowed key from .env (never printed)."""
    if key not in ALLOWED_KEYS:
        sys.exit(f"key {key} not allowed")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return None


def type_text(text: str) -> None:
    # `input text` treats %s as space; other characters are shielded by the quoting.
    shell("input", "text", text.replace(" ", "%s"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--redact-all", action="store_true", help="hide all node text in dump")
    p.add_argument("--password", action="store_true", help="type-key: require the focused node to be a password field")
    p.add_argument("cmd")
    p.add_argument("args", nargs="*")
    a = p.parse_args()
    c, args = a.cmd, a.args
    if c == "shot":
        Path(args[0]).write_bytes(adb("exec-out", "screencap", "-p", binary=True))
        print("saved", args[0])
    elif c == "dump":
        root = dump_xml()
        for n in nodes(root):
            print(fmt(n, a.redact_all))
    elif c == "tap":
        shell("input", "tap", args[0], args[1])
    elif c == "swipe":
        shell("input", "swipe", *args[:4], args[4] if len(args) > 4 else "300")
    elif c == "back":
        shell("input", "keyevent", "KEYCODE_BACK")
    elif c == "home":
        shell("input", "keyevent", "KEYCODE_HOME")
    elif c == "key":
        shell("input", "keyevent", args[0])
    elif c == "type":
        type_text(" ".join(args))
    elif c == "type-key":
        value = env_value(args[0])
        if not value:
            sys.exit(f"{args[0]} empty or unset")
        root = dump_xml()
        if a.password and not focused_is_password(root):
            sys.exit("REFUSED: focused node is not a password field")
        if not a.password and focused_is_password(root):
            sys.exit("REFUSED: focused node IS a password field but --password not given")
        type_text(value)
        print(f"typed {len(value)} chars into {'password' if a.password else 'plain'} field")
    elif c == "start":
        print(shell("am", "start", "-n", args[0]).strip())
    elif c == "pm":
        print(shell("pm", "list", "packages", *args).strip())
    else:
        sys.exit(f"unknown command {c}")


if __name__ == "__main__":
    main()
