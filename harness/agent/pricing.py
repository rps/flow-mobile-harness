"""Price table, cost estimates and the cumulative spend ledger."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from harness.contracts import TokenUsage

log = logging.getLogger(__name__)

PRICING_PATH = Path(__file__).with_name("pricing.json")


@cache
def load_prices(path: str | os.PathLike[str] = PRICING_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def model_price(model: str, path: str | os.PathLike[str] = PRICING_PATH) -> dict[str, float] | None:
    return load_prices(path)["models"].get(model)


def estimate_cost(
    model: str, usage: TokenUsage, path: str | os.PathLike[str] = PRICING_PATH
) -> float | None:
    """Estimated USD for `usage` on `model`; None (and a warning) if unpriced."""
    price = model_price(model, path)
    if price is None:
        log.warning("no price for model %r in %s; cost is unknown", model, path)
        return None
    return (
        usage.input_tokens * price["input"]
        + usage.output_tokens * price["output"]
        + usage.cache_read_input_tokens * price["cache_read"]
        + usage.cache_creation_input_tokens * price["cache_write_5m"]
    ) / 1_000_000


class Ledger:
    """Cumulative estimated spend across runs, kept in a JSON file.

    Writes are atomic (temp file + os.replace) and serialised with an
    exclusive flock on a sidecar lock file.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"total_usd": 0.0, "entries": []}
        return json.loads(self.path.read_text())

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def total(self) -> float:
        with self._locked():
            return float(self._read()["total_usd"])

    def add(self, amount: float, run_label: str = "") -> float:
        """Add `amount` USD and return the new total."""
        with self._locked():
            data = self._read()
            data["total_usd"] = float(data["total_usd"]) + amount
            data["entries"].append(
                {"at": datetime.now(UTC).isoformat(), "run": run_label, "usd": amount}
            )
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
            with os.fdopen(fd, "w") as out:
                json.dump(data, out, indent=1)
            os.replace(tmp, self.path)
            return data["total_usd"]
