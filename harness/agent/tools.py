"""Tool definitions the model sees, input validation, and device dispatch.

request_confirmation and finish are defined here but handled by the loop,
since they talk to the confirmation handler and end the run.
"""

from __future__ import annotations

from typing import Any

from harness.contracts import Device, Verdict


def _closed(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_INT = {"type": "integer"}

# The device's read-only queries (AdbDevice in harness/device/adb.py).
DEFAULT_QUERY_NAMES = ["calendar.events", "contacts.list", "sms.list"]
QUERY_LIMIT_DEFAULT = 50
QUERY_LIMIT_MAX = 200
_STR = {"type": "string"}

# Fixed order: the tools array is part of the cached prefix.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "tap",
        "description": "Tap a point on the screen. x and y are pixels in the latest screenshot.",
        "input_schema": _closed({"x": _INT, "y": _INT}),
        "strict": True,
    },
    {
        "name": "type_text",
        "description": "Type text into the focused field. Tap the field first if nothing is focused.",
        "input_schema": _closed({"text": _STR}),
        "strict": True,
    },
    {
        "name": "swipe",
        "description": (
            "Swipe from (x1, y1) to (x2, y2), in screenshot pixels. "
            "Use it to scroll: swipe upward to reveal content further down."
        ),
        "input_schema": _closed({"x1": _INT, "y1": _INT, "x2": _INT, "y2": _INT}),
        "strict": True,
    },
    {
        "name": "back",
        "description": "Press the Android back button.",
        "input_schema": _closed({}),
        "strict": True,
    },
    {
        "name": "home",
        "description": "Press the Android home button.",
        "input_schema": _closed({}),
        "strict": True,
    },
    {
        "name": "open_app",
        "description": "Launch an installed app by its Android package name, e.g. com.android.settings.",
        "input_schema": _closed({"package": _STR}),
        "strict": True,
    },
    {
        "name": "query_structured",
        "description": "",  # filled in by build_tools from the device's query names
        "input_schema": {
            "type": "object",
            "properties": {
                "name": _STR,
                "params": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": QUERY_LIMIT_MAX}},
                    "additionalProperties": False,
                },
            },
            "required": ["name", "params"],
        },
    },
    {
        "name": "request_confirmation",
        "description": (
            "Required before any sensitive step as defined in the system prompt: financial "
            "(including drafts), outbound, destructive or settings changes, or anything "
            "committing another person or account."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"action": _STR, "summary": {"type": "object"}},
            "required": ["action", "summary"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the task. verdict is done (goal visibly reached), infeasible (cannot be "
            "done here) or failed (tried and could not). summary says what happened."
        ),
        "input_schema": _closed(
            {"verdict": {"type": "string", "enum": [v.value for v in Verdict]}, "summary": _STR}
        ),
        "strict": True,
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_DEFINITIONS]


def query_description(names: list[str]) -> str:
    return (
        "Run a read-only data query on the device instead of reading the data off the screen. "
        f"The only query names are: {', '.join(names)}. "
        f'The only parameter is an optional integer "limit" (1..{QUERY_LIMIT_MAX}, '
        f"default {QUERY_LIMIT_DEFAULT}); pass params as {{}} or {{\"limit\": n}}. "
        "There is no search or filter parameter: fetch the rows and filter them yourself. "
        "The result has rows (up to limit) and total, the number of rows that exist."
    )


def build_tools(query_names: list[str] | None = None) -> list[dict[str, Any]]:
    """TOOL_DEFINITIONS with the query_structured description naming the
    device's queries. Call once per run; the result is part of the cached prefix."""
    names = sorted(query_names) if query_names else DEFAULT_QUERY_NAMES
    return [
        {**t, "description": query_description(names)} if t["name"] == "query_structured" else t
        for t in TOOL_DEFINITIONS
    ]

_FIELDS: dict[str, dict[str, type]] = {
    "tap": {"x": int, "y": int},
    "type_text": {"text": str},
    "swipe": {"x1": int, "y1": int, "x2": int, "y2": int},
    "back": {},
    "home": {},
    "open_app": {"package": str},
    "query_structured": {"name": str, "params": dict},
    "request_confirmation": {"action": str, "summary": dict},
    "finish": {"verdict": str, "summary": str},
}


def validate_input(name: str, tool_input: Any) -> str | None:
    """Return an error message, or None if the input is usable."""
    fields = _FIELDS.get(name)
    if fields is None:
        return f"unknown tool {name!r}"
    if not isinstance(tool_input, dict):
        return "input must be an object"
    for key, typ in fields.items():
        if key not in tool_input:
            return f"missing field {key!r}"
        value = tool_input[key]
        # bool is a subclass of int; a coordinate of True is not valid.
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            return f"field {key!r} must be {typ.__name__}"
    extra = set(tool_input) - set(fields)
    if extra:
        return f"unexpected fields {sorted(extra)}"
    if name == "finish" and tool_input["verdict"] not in {v.value for v in Verdict}:
        return f"verdict must be one of {[v.value for v in Verdict]}"
    if name == "request_confirmation" and not tool_input["action"].strip():
        return "action must not be empty"
    return None


def execute(device: Device, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Run a device-facing tool. Raises DeviceError (incl. QueryNotAllowed)."""
    if name == "tap":
        device.tap(tool_input["x"], tool_input["y"])
    elif name == "type_text":
        device.type_text(tool_input["text"])
    elif name == "swipe":
        device.swipe(tool_input["x1"], tool_input["y1"], tool_input["x2"], tool_input["y2"])
    elif name == "back":
        device.back()
    elif name == "home":
        device.home()
    elif name == "open_app":
        device.open_app(tool_input["package"])
    elif name == "query_structured":
        return {"ok": True, "result": device.query_structured(tool_input["name"], tool_input["params"])}
    else:
        raise ValueError(f"{name} is not a device tool")
    return {"ok": True}
