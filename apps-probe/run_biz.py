"""Run one business task on the business profile until the CLI grows a --profile hook.

    PYTHONPATH=<worktree> python apps-probe/run_biz.py biz_b1_hours [--confirm approve] [--seed N]

Runs from a worktree: the harness code is the worktree's, while .env and the
runs/ directory are the repo root's (LABS_REPO_ROOT, default: the parent of
this checkout's apps-probe/ when it is the main checkout, else set it).
Goes through business.run_scored(), which pins the profile's baseline JSON
and refuses freeform goals.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(os.environ.get("LABS_REPO_ROOT", HERE))

from harness.cli import format_line  # noqa: E402
from harness.contracts import Config  # noqa: E402
from harness.emulator.profiles import business  # noqa: E402  (binds the manager before anything imports it)
from harness.tasks import biz_b1_hours, biz_b2_rate, biz_d_invoice, biz_h  # noqa: E402
from harness.trace.store import TraceStore  # noqa: E402

BIZ_TASKS = (biz_b1_hours.TASK, biz_b2_rate.TASK, biz_d_invoice.TASK, biz_h.TASK)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("task", choices=[t.id for t in BIZ_TASKS])
    p.add_argument("--confirm", default="approve")
    p.add_argument("--seed", type=int)
    p.add_argument("--no-window", action="store_true")
    a = p.parse_args(argv)
    if not (REPO_ROOT / ".env").exists():
        sys.exit(f"no .env under {REPO_ROOT}; set LABS_REPO_ROOT to the main checkout")
    config = Config.from_env(env_file=REPO_ROOT / ".env", environ={"HARNESS_RUNS_DIR": str(REPO_ROOT / "runs")})
    store = TraceStore(config.runs_dir)
    run = business.run_scored(a.task, config, a.confirm, store, windowed=not a.no_window, seed=a.seed,
                              meta={"worktree": str(HERE)})
    print(format_line(run))
    print(f"  replay: {store.run_dir(run.run_id) / 'replay.html'}")
    print(f"  usage: {run.usage.to_dict()} cost_usd={run.estimated_cost_usd} steps={run.meta.get('steps')} error={run.meta.get('error')}")
    return 1 if run.meta.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
