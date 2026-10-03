"""Print one run's step list and verifier detail for hand audit (read-only).

    python docs/audit_run.py <run_id> [--runs-dir runs] [--tree STEP]

--tree STEP also prints the UI dump saved for that step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.trace.store import TraceStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_id")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--tree", type=int)
    a = ap.parse_args(argv)
    store = TraceStore(a.runs_dir)
    run, steps = store.load_run(a.run_id)
    print(f"{run.run_id}  task={run.task_id}  seed={run.seed}  model={run.model}")
    print(f"goal: {run.goal}")
    print(f"verdict={run.agent_verdict.value if run.agent_verdict else None}  "
          f"termination={run.termination_reason.value if run.termination_reason else None}  "
          f"cost=${run.estimated_cost_usd}  wall={run.meta.get('wall_s')}s  steps={len(steps)}")
    print(f"summary: {run.agent_summary}\n")
    for s in steps:
        inp = json.dumps(s.tool_input, ensure_ascii=False)
        res = json.dumps(s.tool_result, ensure_ascii=False)
        print(f"[{s.index:02d}] {s.tool_name} {inp[:200]}")
        print(f"     -> {res[:200]}  ({s.duration_ms} ms)")
        if s.reasoning:
            print(f"     reasoning: {s.reasoning[:300]}")
    v = run.verifier_result
    print()
    if v is None:
        print("verifier: none")
    else:
        print(f"verifier: {'PASS' if v.passed else 'FAIL'}  tier={int(v.oracle_tier)}  self_report_agrees={v.self_report_agrees}")
        for group, checks in (("end_state", v.end_state), ("side_effects", v.side_effects), ("process", v.process)):
            for c in checks:
                print(f"  {group:<12} {'ok  ' if c.passed else 'FAIL'} {c.name}: {c.detail}")
    if a.tree is not None:
        step = next((s for s in steps if s.index == a.tree), None)
        if step is None:
            print(f"no step {a.tree} in this run (steps 0..{len(steps) - 1})", file=sys.stderr)
            return 1
        if not step.ui_tree_path:
            print(f"step {a.tree} has no UI tree recorded", file=sys.stderr)
            return 1
        print(f"\n--- UI tree step {a.tree} ---")
        print((store.run_dir(run.run_id) / step.ui_tree_path).read_text(errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
