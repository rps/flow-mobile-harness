"""Scoreboard: aggregate every runs/<run_id>/run.json.

Fake and real runs are always kept in separate groups. Every rate comes as
{"rate", "n"} where n is its denominator; rate is None when n is 0.
Freeform runs have no verifier, so they count in `runs` but not in any
verifier-based denominator.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness.contracts import RunRecord, Verdict
from harness.trace.store import RUN_FILE

log = logging.getLogger(__name__)


def load_runs(runs_dir: str | os.PathLike[str]) -> list[RunRecord]:
    """Every readable run.json under runs_dir; unreadable ones are skipped."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    runs: list[RunRecord] = []
    for p in sorted(root.iterdir()):
        f = p / RUN_FILE
        if not f.is_file():
            continue
        try:
            runs.append(RunRecord.from_dict(json.loads(f.read_text())))
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("skipping %s: %s", f, e)
    return runs


def _rate(hits: int, n: int) -> dict[str, Any]:
    return {"rate": None if n == 0 else hits / n, "n": n}


def _mean(values: list[float]) -> dict[str, Any]:
    return {"mean": None if not values else sum(values) / len(values), "n": len(values)}


def summarise(runs: list[RunRecord]) -> dict[str, Any]:
    """Counts and rates for one group of runs."""
    verified = [r for r in runs if r.verifier_result is not None]
    passed = sum(r.verifier_result.passed for r in verified)
    said_done = sum(r.agent_verdict == Verdict.DONE for r in runs)
    false_success = sum(r.agent_verdict == Verdict.DONE and not r.verifier_result.passed for r in verified)
    false_failure = sum(r.agent_verdict != Verdict.DONE and r.verifier_result.passed for r in verified)
    return {
        "runs": len(runs),
        "verified_runs": len(verified),
        "verifier_pass": _rate(passed, len(verified)),
        "agent_said_done": _rate(said_done, len(runs)),
        "false_success": {"count": false_success, "n": len(verified)},
        "false_failure": {"count": false_failure, "n": len(verified)},
        "steps": _mean([float(r.meta.get("steps", 0)) for r in runs]),
        "cost_usd": _mean([r.estimated_cost_usd for r in runs if r.estimated_cost_usd is not None]),
        "wall_s": _mean([float(r.meta["wall_s"]) for r in runs if "wall_s" in r.meta]),
        "errors": sum(bool(r.meta.get("error")) for r in runs),
    }


def _ledger_total(runs_dir: Path) -> float:
    path = runs_dir / "ledger.json"
    try:
        return float(json.loads(path.read_text())["total_usd"]) if path.is_file() else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("ledger %s unreadable", path)
        return 0.0


def _group(runs: list[RunRecord]) -> dict[str, Any]:
    by_flow: dict[str, list[RunRecord]] = defaultdict(list)
    by_task: dict[str, list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_flow[r.flow_type.value].append(r)
        by_task[r.task_id or "freeform"].append(r)
    return {
        "by_flow": [{"flow_type": k, **summarise(v)} for k, v in sorted(by_flow.items())],
        "by_task": [
            {"task_id": k, "flow_type": v[0].flow_type.value,
             "oracle_tier": next((int(r.verifier_result.oracle_tier) for r in v if r.verifier_result), None),
             **summarise(v)}
            for k, v in sorted(by_task.items())
        ],
        "totals": summarise(runs),
    }


def scoreboard(runs_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """{"real": group, "fake": group, "ledger_total_usd": float}."""
    runs = load_runs(runs_dir)
    return {
        "real": _group([r for r in runs if not r.meta.get("fake")]),
        "fake": _group([r for r in runs if r.meta.get("fake")]),
        "ledger_total_usd": _ledger_total(Path(runs_dir)),
    }
