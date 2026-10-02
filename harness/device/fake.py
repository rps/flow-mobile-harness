"""In-memory Device for tests: canned screens and query results, recorded actions."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from harness.contracts import DeviceError, QueryNotAllowed, Screenshot
from harness.device.adb import scale_factor


class FakeDevice:
    """Implements the Device protocol without a device.

    - `trees`: ui_tree() returns them in order and then keeps returning the last.
    - `query_results`: name -> result dict; its keys are the allow-list.
    - `installed`: packages open_app accepts.
    - `fail`: method name -> exception to raise instead of acting.
    - `actions`: every action as (name, args), in call order, including query_structured;
      screenshot() and ui_tree() are not recorded.
    """

    def __init__(
        self,
        width: int = 1080,
        height: int = 2400,
        max_px: int = 1280,
        trees: list[str] | None = None,
        query_results: dict[str, dict[str, Any]] | None = None,
        installed: tuple[str, ...] = ("net.gsantner.markor",),
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.width, self.height = width, height
        f = scale_factor(width, height, max_px)
        self.scaled = (max(1, round(width * f)), max(1, round(height * f)))
        self.trees = list(trees or ["screen (fake)"])
        self.query_results = dict(query_results or {})
        self.installed = set(installed)
        self.fail = dict(fail or {})
        self.actions: list[tuple[str, tuple]] = []

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def _act(self, name: str, *args: Any) -> None:
        self._check(name)
        self.actions.append((name, args))

    def screenshot(self) -> Screenshot:
        self._check("screenshot")
        shade = (len(self.actions) * 40) % 256
        buf = io.BytesIO()
        Image.new("RGB", self.scaled, (shade, shade, shade)).save(buf, format="PNG")
        return Screenshot(buf.getvalue(), self.width, self.height, *self.scaled)

    def ui_tree(self) -> str:
        self._check("ui_tree")
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def tap(self, x: int, y: int) -> None:
        self._act("tap", x, y)

    def type_text(self, text: str) -> None:
        self._act("type_text", text)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._act("swipe", x1, y1, x2, y2, duration_ms)

    def back(self) -> None:
        self._act("back")

    def home(self) -> None:
        self._act("home")

    def open_app(self, package: str) -> None:
        self._check("open_app")
        if package not in self.installed:
            raise DeviceError(f"package not installed: {package}")
        self.actions.append(("open_app", (package,)))

    def allowed_queries(self) -> list[str]:
        return list(self.query_results)

    def query_structured(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self._check("query_structured")
        if name not in self.query_results:
            raise QueryNotAllowed(f"query not allowed: {name!r}")
        self.actions.append(("query_structured", (name, dict(params or {}))))
        return self.query_results[name]
