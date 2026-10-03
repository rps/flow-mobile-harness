"""Read-only benchmark tables from runs/<run_id>/run.json (Markdown to stdout).

    python docs/benchmark_tables.py [--runs-dir runs] [--since 20261003-000000] [--ids FILE]

Uses harness.scoreboard.aggregate as a library; never writes under runs/.
Selection: real (non-fake) task runs only; freeform runs and runs whose meta
has `cancelled` or `cancelled_by` are dropped. --since keeps run ids at or
after the given prefix; --ids keeps only the run ids listed one per line.
Denominators follow aggregate.summarise: a verifier rate counts runs that
have a verifier result; task runs that ended before the verifier ran are
reported in the "unverified" column and listed in the failure taxonomy.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.contracts import RunRecord, StepRecord, Verdict  # noqa: E402
from harness.scoreboard.aggregate import _group, load_runs  # noqa: E402
from harness.trace.store import TraceStore  # noqa: E402

# Process-check names used by the SMS task (harness.verify.process) and by the
# sample-app / business tasks (gate_checks in their verifiers).
GATE_CHECKS = {"approved_confirmation_requested", "no_send_before_approval", "confirmation_before_sensitive_action"}
PREPARED_CHECKS = {"summary_matches_prepared_state"}
EXECUTED_CHECKS = {"sent_matches_approved_summary", "executed_matches_approved_summary"}
# Detail strings those executed checks return when no sensitive action happened.
NOTHING_EXECUTED = re.compile(r"^(nothing sent|no \w+ placed|no \w+ sent)$")

Steps = dict[str, list[StepRecord]]


def _fmt(value, spec: str, none: str = "-") -> str:
    return none if value is None else format(value, spec)


def _pct(rate) -> str:
    return "-" if rate is None else f"{100 * rate:.0f}%"


def _select(runs: list[RunRecord], since: str | None, ids: set[str] | None) -> list[RunRecord]:
    out = [r for r in runs if not r.meta.get("fake") and r.task_id is not None
           and not r.meta.get("cancelled") and not r.meta.get("cancelled_by")]
    if since:
        out = [r for r in out if r.run_id >= since]
    if ids is not None:
        out = [r for r in out if r.run_id in ids]
    return out


def _load_steps(runs: list[RunRecord], runs_dir: Path) -> Steps:
    store = TraceStore(runs_dir)
    return {r.run_id: store.load_run(r.run_id)[1] for r in runs}


def _tags(run_id: str, runs_dir: Path) -> list[str]:
    """Manual failure tags from runs/<id>/tags.json (area C's shape), if any."""
    path = runs_dir / run_id / "tags.json"
    try:
        tags = json.loads(path.read_text()).get("tags", []) if path.is_file() else []
    except (OSError, ValueError, AttributeError):
        return []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _failed(r: RunRecord) -> bool:
    return r.verifier_result is None or not r.verifier_result.passed


def _failure_label(r: RunRecord, tags: list[str]) -> str:
    """Taxonomy bucket: termination reason (plus harness stage for errors) and manual tags."""
    reason = r.termination_reason.value if r.termination_reason else "unfinished"
    if r.meta.get("error"):
        stage = str(r.meta["error"]).split(":", 1)[0]
        label = f"harness error during {stage}"
    elif reason != "finished":
        label = f"terminated: {reason}"
    elif r.verifier_result is None:
        label = "finished, no verifier result"
    else:
        verdict = r.agent_verdict.value if r.agent_verdict else "none"
        label = f"verifier fail, agent said {verdict}"
    return label + (f" [{', '.join(sorted(tags))}]" if tags else "")


def per_task_table(runs: list[RunRecord]) -> str:
    group = _group(runs)
    unverified = Counter(r.task_id for r in runs if r.verifier_result is None)
    lines = [
        "| task | flow | tier | n | verified | verifier pass | agent said done | false success | false failure | mean steps | mean cost $ | mean wall s | errors | unverified |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def row(label: str, s: dict, flow: str = "", tier=None, unv: int = 0) -> str:
        vp = s["verifier_pass"]
        passed = round(vp["rate"] * vp["n"]) if vp["rate"] is not None else 0
        done = round(s["agent_said_done"]["rate"] * s["runs"]) if s["agent_said_done"]["rate"] is not None else 0
        return (f"| {label} | {flow} | {_fmt(tier, 'd', '')} | {s['runs']} | {s['verified_runs']} "
                f"| {passed}/{vp['n']} ({_pct(vp['rate'])}) | {done}/{s['runs']} "
                f"| {s['false_success']['count']} | {s['false_failure']['count']} "
                f"| {_fmt(s['steps']['mean'], '.1f')} | {_fmt(s['cost_usd']['mean'], '.3f')} "
                f"| {_fmt(s['wall_s']['mean'], '.0f')} | {s['errors']} | {unv} |")

    for t in group["by_task"]:
        lines.append(row(t["task_id"], t, t["flow_type"], t["oracle_tier"], unverified.get(t["task_id"], 0)))
    lines.append(row("**all**", group["totals"], unv=sum(unverified.values())))
    return "\n".join(lines)


def run_table(runs: list[RunRecord]) -> str:
    lines = ["| run id | task | seed | steps | cost $ | wall s | verdict | verifier | termination |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda r: r.run_id):
        v = r.verifier_result
        ver = "-" if v is None else ("PASS" if v.passed else "FAIL")
        lines.append(
            f"| {r.run_id} | {r.task_id} | {_fmt(r.seed, 'd')} | {r.meta.get('steps', 0)} "
            f"| {_fmt(r.estimated_cost_usd, '.3f')} | {_fmt(r.meta.get('wall_s'), '.0f')} "
            f"| {r.agent_verdict.value if r.agent_verdict else 'none'} | {ver} "
            f"| {r.termination_reason.value if r.termination_reason else 'unfinished'} |"
        )
    return "\n".join(lines)


def taxonomy(runs: list[RunRecord], runs_dir: Path) -> str:
    buckets: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        if _failed(r):
            buckets[_failure_label(r, _tags(r.run_id, runs_dir))].append(r.run_id)
    if not buckets:
        return "No failing runs."
    lines = ["| failure bucket | count | run ids |", "|---|---|---|"]
    for label, ids in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"| {label} | {len(ids)} | {', '.join(ids)} |")
    return "\n".join(lines)


def headline_split(runs: list[RunRecord]) -> str:
    groups: dict[str, list[RunRecord]] = {"headline (tiers 1-3)": [], "non-headline (tiers 4-6)": []}
    for r in runs:
        if r.verifier_result is not None:
            key = "headline (tiers 1-3)" if r.verifier_result.oracle_tier.is_headline else "non-headline (tiers 4-6)"
            groups[key].append(r)
    lines = ["| oracle group | n | verifier pass | tiers present |", "|---|---|---|---|"]
    for key, group in groups.items():
        passed = sum(r.verifier_result.passed for r in group)
        rate = None if not group else passed / len(group)
        tiers = sorted({int(r.verifier_result.oracle_tier) for r in group})
        lines.append(f"| {key} | {len(group)} | {passed}/{len(group)} ({_pct(rate)}) | {tiers or '-'} |")
    return "\n".join(lines)


def disagreement(runs: list[RunRecord]) -> str:
    rows = [r for r in runs if r.verifier_result is not None and r.verifier_result.self_report_agrees is False]
    if not rows:
        return "No run where the agent's verdict disagreed with the verifier (among runs that reached a verdict)."
    lines = ["| run id | task | agent verdict | verifier | direction |", "|---|---|---|---|---|"]
    for r in rows:
        direction = ("false success (said done, verifier failed)" if r.agent_verdict == Verdict.DONE
                     else "false failure (did not say done, verifier passed)")
        lines.append(f"| {r.run_id} | {r.task_id} | {r.agent_verdict.value} | {'PASS' if r.verifier_result.passed else 'FAIL'} | {direction} |")
    return "\n".join(lines)


def _has_sensitive_actions(task_id: str) -> bool:
    try:
        from harness.tasks import registry
        return bool(getattr(registry.get(task_id), "sensitive_actions", None))
    except Exception:  # task unknown to this checkout: decide from check names alone
        return False


def gate_accuracy(runs: list[RunRecord], steps: Steps) -> str:
    """Confirmation-gate accuracy per gated task.

    A task is gated when its TaskSpec declares sensitive_actions or its
    process checks include a gate check. Columns:
      gated       all gate checks passed (confirmation approved before the action)
      prepared    summary_matches_prepared_state passed ("-" when the task has no such check)
      executed    the executed/sent-matches-approved-summary check passed
      false appr  an approved request_confirmation exists, the action was
                  executed, and executed did not pass
      ungated     the action was executed and the gate checks did not all pass
    "Executed" is read from the executed check's detail: it reports
    "nothing sent" / "no order placed" when no sensitive action happened.
    """
    rows: dict[str, Counter] = defaultdict(Counter)
    has_prepared: set[str] = set()
    for r in runs:
        v = r.verifier_result
        if v is None:
            continue
        proc = {c.name: c for c in v.process}
        names = set(proc)
        if not (names & GATE_CHECKS or _has_sensitive_actions(r.task_id)):
            continue
        t = rows[r.task_id]
        t["n"] += 1
        gate = [proc[n].passed for n in names & GATE_CHECKS]
        gated = bool(gate) and all(gate)
        executed_checks = [proc[n] for n in names & EXECUTED_CHECKS]
        executed_ok = bool(executed_checks) and all(c.passed for c in executed_checks)
        acted = any(c.passed or not NOTHING_EXECUTED.match(c.detail.strip()) for c in executed_checks)
        if names & PREPARED_CHECKS:
            has_prepared.add(r.task_id)
            t["prepared"] += all(proc[n].passed for n in names & PREPARED_CHECKS)
        approved = any(s.tool_name == "request_confirmation" and s.tool_result.get("decision") == "approve"
                       for s in steps.get(r.run_id, []))
        t["gated"] += gated
        t["executed"] += executed_ok
        t["false_approval"] += approved and acted and not executed_ok
        t["ungated"] += acted and not gated
    if not rows:
        return "No gated task in this selection."
    lines = ["| task | n | gate before action | approved summary matched prepared state | executed action matched approved summary | false approvals | ungated actions |",
             "|---|---|---|---|---|---|---|"]
    for task_id, t in sorted(rows.items()):
        prepared = f"{t['prepared']}/{t['n']}" if task_id in has_prepared else "- (no check)"
        lines.append(f"| {task_id} | {t['n']} | {t['gated']}/{t['n']} | {prepared} | {t['executed']}/{t['n']} "
                     f"| {t['false_approval']} | {t['ungated']} |")
    return "\n".join(lines)


def tool_mix(runs: list[RunRecord], steps: Steps) -> str:
    counts: dict[str, Counter] = defaultdict(Counter)
    for r in runs:
        for s in steps.get(r.run_id, []):
            counts[r.task_id][s.tool_name] += 1
    tools = sorted({t for c in counts.values() for t in c})
    lines = ["| task | " + " | ".join(tools) + " |", "|---|" + "---|" * len(tools)]
    for task_id, c in sorted(counts.items()):
        lines.append(f"| {task_id} | " + " | ".join(str(c.get(t, 0)) for t in tools) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--since")
    ap.add_argument("--ids", help="file with one run id per line")
    a = ap.parse_args(argv)
    runs_dir = Path(a.runs_dir)
    ids = set(Path(a.ids).read_text().split()) if a.ids else None
    runs = _select(load_runs(runs_dir), a.since, ids)
    steps = _load_steps(runs, runs_dir)
    total_cost = sum(r.estimated_cost_usd or 0.0 for r in runs)
    print(f"Selected {len(runs)} real task runs, estimated spend ${total_cost:.4f}\n")
    for title, body in (
        ("Per task", per_task_table(runs)),
        ("Per run", run_table(runs)),
        ("Failure taxonomy", taxonomy(runs, runs_dir)),
        ("Self-report disagreement", disagreement(runs)),
        ("Headline vs non-headline oracle", headline_split(runs)),
        ("Gate accuracy", gate_accuracy(runs, steps)),
        ("Tool mix (steps per tool)", tool_mix(runs, steps)),
    ):
        print(f"### {title}\n\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
