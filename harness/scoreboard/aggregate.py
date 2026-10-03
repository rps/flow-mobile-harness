"""Scoreboard: aggregate every runs/<run_id>/run.json.

Fake and real runs are always kept in separate groups. Every rate comes as
{"rate", "n"} where n is its denominator; rate is None when n is 0.
Freeform runs have no verifier, so they count in `runs` but not in any
verifier-based denominator.

Failure-taxonomy tags are set by hand per run (web UI, Runs tab) and kept in
runs/<run_id>/tags.json; each group counts how many of its runs carry each tag.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.contracts import FlowType, RunRecord, Verdict
from harness.tasks import registry
from harness.trace.store import RUN_FILE

log = logging.getLogger(__name__)

TAGS_FILE = "tags.json"
FAILURE_TAGS = ("wrong_element", "lost_state", "login_wall", "file_handoff", "false_success",
                "missed_confirmation")


def load_runs(runs_dir: str | os.PathLike[str]) -> list[RunRecord]:
    """Every readable run.json under runs_dir; unreadable ones are skipped."""
    return [run for run, _ in load_runs_with_tags(runs_dir)]


def load_runs_with_tags(runs_dir: str | os.PathLike[str]) -> list[tuple[RunRecord, list[str]]]:
    """One pass over runs_dir: (run, its failure tags) for every readable run.json."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    runs: list[tuple[RunRecord, list[str]]] = []
    for p in sorted(root.iterdir()):
        f = p / RUN_FILE
        if not f.is_file():
            continue
        try:
            run = RunRecord.from_dict(json.loads(f.read_text()))
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("skipping %s: %s", f, e)
            continue
        runs.append((run, load_tags(p)["tags"]))
    return runs


def load_tags(run_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """{"tags": [...], "updated_at": str | None} from run_dir/tags.json; unknown
    tags are dropped, and a missing or unreadable file reads as no tags."""
    path = Path(run_dir) / TAGS_FILE
    if not path.is_file():
        return {"tags": [], "updated_at": None}
    try:
        data = json.loads(path.read_text())
        tags = set(data.get("tags", []))
        return {"tags": [t for t in FAILURE_TAGS if t in tags], "updated_at": data.get("updated_at")}
    except (OSError, ValueError, AttributeError, TypeError) as e:
        log.warning("tags %s unreadable: %s", path, e)
        return {"tags": [], "updated_at": None}


def save_tags(run_dir: str | os.PathLike[str], tags: list[str]) -> dict[str, Any]:
    """Replace run_dir/tags.json atomically. Raises ValueError for an unknown tag."""
    unknown = sorted(set(tags) - set(FAILURE_TAGS))
    if unknown:
        raise ValueError(f"unknown tags {unknown}; expected some of {list(FAILURE_TAGS)}")
    out = {"tags": [t for t in FAILURE_TAGS if t in tags], "updated_at": datetime.now(UTC).isoformat()}
    path = Path(run_dir) / TAGS_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return out


def _rate(hits: int, n: int) -> dict[str, Any]:
    return {"rate": None if n == 0 else hits / n, "n": n}


def _mean(values: list[float]) -> dict[str, Any]:
    return {"mean": None if not values else sum(values) / len(values), "n": len(values)}


def _expected_verdict(run: RunRecord) -> Verdict:
    """Same rule as harness.verify.runner.expected_verdict, from the run's own
    flow type: INFEASIBLE for a flow-H task, DONE otherwise."""
    return Verdict.INFEASIBLE if run.flow_type is FlowType.H else Verdict.DONE


def summarise(runs: list[RunRecord], tags: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Counts and rates for one group of runs; `tags` maps run_id to its failure tags.

    false_success: the agent gave the task's expected verdict but the verifier
    failed; false_failure: the verifier passed but the agent did not give the
    expected verdict. Both are the runs where self_report_agrees is false."""
    verified = [r for r in runs if r.verifier_result is not None]
    passed = sum(r.verifier_result.passed for r in verified)
    said_done = sum(r.agent_verdict == Verdict.DONE for r in runs)
    false_success = sum(r.agent_verdict == _expected_verdict(r) and not r.verifier_result.passed for r in verified)
    false_failure = sum(r.agent_verdict != _expected_verdict(r) and r.verifier_result.passed for r in verified)
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
        "failure_tags": {t: sum(t in (tags or {}).get(r.run_id, []) for r in runs) for t in FAILURE_TAGS},
    }


def _ledger_total(runs_dir: Path) -> float:
    path = runs_dir / "ledger.json"
    try:
        return float(json.loads(path.read_text())["total_usd"]) if path.is_file() else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("ledger %s unreadable", path)
        return 0.0


def _task_tier(task_id: str) -> int | None:
    try:
        return int(registry.get(task_id).oracle_tier)
    except KeyError:
        return None


def _group(runs: list[RunRecord], tags: dict[str, list[str]]) -> dict[str, Any]:
    by_flow: dict[str, list[RunRecord]] = defaultdict(list)
    by_task: dict[str, list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_flow[r.flow_type.value].append(r)
        by_task[r.task_id or "freeform"].append(r)
    return {
        "by_flow": [{"flow_type": k, **summarise(v, tags)} for k, v in sorted(by_flow.items())],
        "by_task": [
            {"task_id": k, "flow_type": v[0].flow_type.value,
             "oracle_tier": next((int(r.verifier_result.oracle_tier) for r in v if r.verifier_result),
                                 None if k == "freeform" else _task_tier(k)),
             **summarise(v, tags)}
            for k, v in sorted(by_task.items())
        ],
        "totals": summarise(runs, tags),
    }


def scoreboard(runs_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """{"real": group, "fake": group, "ledger_total_usd": float}."""
    loaded = load_runs_with_tags(runs_dir)
    runs = [r for r, _ in loaded]
    tags = {r.run_id: t for r, t in loaded}
    return {
        "real": _group([r for r in runs if not r.meta.get("fake")], tags),
        "fake": _group([r for r in runs if r.meta.get("fake")], tags),
        "ledger_total_usd": _ledger_total(Path(runs_dir)),
    }
