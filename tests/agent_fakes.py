"""Test doubles for the agent loop: a fake Device and a scripted model client.

ScriptedModel returns real anthropic SDK Message objects and snapshots every
request as JSON at call time (the loop mutates history in place later).
"""

from __future__ import annotations

import json
from typing import Any

from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from harness.contracts import DeviceError, QueryNotAllowed, Screenshot


class FakeDevice:
    def __init__(self, fail_actions: int = 0, queries: dict[str, dict] | None = None, fail_screenshots: int = 0):
        self.calls: list[tuple] = []
        self.frame = 0
        self.fail_actions = fail_actions
        self.fail_screenshots = fail_screenshots
        self.queries = queries or {}

    def screenshot(self) -> Screenshot:
        if self.fail_screenshots:
            self.fail_screenshots -= 1
            raise DeviceError("screencap failed")
        self.frame += 1
        return Screenshot(png=f"PNG-{self.frame}".encode(), width=1080, height=2400, scaled_width=540, scaled_height=1200)

    def ui_tree(self) -> str:
        return f"<node text='frame {self.frame}'/>"

    def _act(self, *call: Any) -> None:
        self.calls.append(call)
        if self.fail_actions:
            self.fail_actions -= 1
            raise DeviceError(f"{call[0]} failed")

    def tap(self, x: int, y: int) -> None:
        self._act("tap", x, y)

    def type_text(self, text: str) -> None:
        self._act("type_text", text)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._act("swipe", x1, y1, x2, y2)

    def back(self) -> None:
        self._act("back")

    def home(self) -> None:
        self._act("home")

    def open_app(self, package: str) -> None:
        self._act("open_app", package)

    def allowed_queries(self) -> list[str]:
        return sorted(self.queries)

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("query_structured", name, params))
        if name not in self.queries:
            raise QueryNotAllowed(name)
        unknown = set(params) - {"limit"}
        if unknown:  # as AdbDevice does
            raise DeviceError(f"unknown parameters: {', '.join(sorted(unknown))}")
        return self.queries[name]


_n = 0


def tool(
    _name: str,
    /,
    *,
    _text: str = "",
    _thinking: str = "",
    _usage: tuple[int, int, int, int] = (100, 20, 0, 0),
    **tool_input: Any,
) -> Message:
    """A response with one tool call. _usage = (input, output, cache_read, cache_write)."""
    global _n
    _n += 1
    content: list[Any] = []
    if _thinking:
        content.append(ThinkingBlock(type="thinking", thinking=_thinking, signature="sig"))
    if _text:
        content.append(TextBlock(type="text", text=_text))
    content.append(ToolUseBlock(type="tool_use", id=f"toolu_{_n}", name=_name, input=tool_input))
    return _message(content, "tool_use", _usage)


def text_only(text: str, usage: tuple[int, int, int, int] = (100, 20, 0, 0)) -> Message:
    return _message([TextBlock(type="text", text=text)], "end_turn", usage)


def _message(content: list[Any], stop_reason: str, usage: tuple[int, int, int, int]) -> Message:
    i, o, cr, cw = usage
    return Message(
        id=f"msg_{_n}",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw),
    )


def check_error_results_text_only(request: dict[str, Any]) -> None:
    """Mirror the API's 400: 'all content must be type text if is_error is true'."""
    for i, msg in enumerate(request["messages"]):
        for j, block in enumerate(msg["content"] if isinstance(msg["content"], list) else []):
            if block.get("type") == "tool_result" and block.get("is_error"):
                inner = block["content"]
                if isinstance(inner, list) and any(b.get("type") != "text" for b in inner):
                    raise AssertionError(f"messages.{i}.content.{j}: is_error tool_result has non-text content")


class ScriptedModel:
    """Stands in for anthropic.Anthropic: `.messages.create(**kw)` returns the
    next scripted response (or raises it if it is an exception)."""

    def __init__(self, responses: list[Any], repeat_last: bool = False):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        check_error_results_text_only(self.requests[-1])
        if len(self.responses) == 1 and self.repeat_last:
            item = self.responses[0]
            if isinstance(item, Message):  # fresh tool_use id each turn
                item = item.model_copy(deep=True)
                for block in item.content:
                    if block.type == "tool_use":
                        global _n
                        _n += 1
                        block.id = f"toolu_{_n}"
        else:
            item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
