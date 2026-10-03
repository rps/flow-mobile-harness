"""Command line for the harness.

    python -m harness.cli run --task a_markor_note [--repeat N] [--confirm prompt|approve|reject] [--windowed] [--fake]
    python -m harness.cli run --all
    python -m harness.cli run --goal "Open Markor and make a shopping list" [--allow-unblocked]
    python -m harness.cli replay <run_id>
    python -m harness.cli ledger
    python -m harness.cli tasks
    python -m harness.cli selftest [--serial S] [--task ID] ...

--fake runs the whole sequence with an in-memory device, inspector and
emulator and a scripted model: no emulator, no API spend.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from harness.contracts import Config, ConfigError, RunRecord
from harness.runner import POLICIES, RunnerError, confirm_policy, run_task
from harness.tasks import registry
from harness.trace.replay import render_replay
from harness.trace.store import TraceStore

log = logging.getLogger(__name__)


@dataclasses.dataclass
class Env:
    """What one invocation runs against."""

    config: Config
    emulator: Any
    device_factory: Callable[[], Any]
    inspector_factory: Callable[[], Any]
    model_client_factory: Callable[[], Any] = lambda: None  # None: run_agent builds the real client
    settings: Any = None
    fake: bool = False


def fake_env(config: Config) -> Env:
    from harness.agent.loop import AgentSettings
    from harness.fake_env import FAKE_MODEL, FakeEmulator, MemoryInspector, ScriptedClient, make_fake_device

    inspector = MemoryInspector()
    return Env(
        config=dataclasses.replace(config, model=FAKE_MODEL),
        emulator=FakeEmulator(inspector),
        device_factory=make_fake_device,
        inspector_factory=lambda: inspector,
        model_client_factory=ScriptedClient,
        settings=AgentSettings(allow_unpriced=True),
        fake=True,
    )


def real_env(config: Config, windowed: bool) -> Env:
    from harness.device.adb import AdbDevice
    from harness.device.inspect import Inspector
    from harness.emulator import manager

    if not manager._online():
        boot_s = manager.start(windowed=windowed)
        print(f"emulator {manager.SERIAL} booted in {boot_s:.1f}s")
    return Env(
        config=config,
        emulator=manager,
        device_factory=lambda: AdbDevice(manager.SERIAL, config),
        inspector_factory=lambda: Inspector(manager.SERIAL),
    )


def _verifier_label(run: RunRecord) -> str:
    if run.verifier_result is None:
        return "unverified" if run.task_id is None else "none"
    return "PASS" if run.verifier_result.passed else "FAIL"


def _row(run: RunRecord) -> list[str]:
    cost = "n/a" if run.estimated_cost_usd is None else f"${run.estimated_cost_usd:.4f}"
    reason = run.termination_reason.value if run.termination_reason else "unfinished"
    return [
        run.run_id,
        run.task_id or "freeform",
        f"{run.meta.get('wall_s', 0.0):.1f}s",
        str(run.meta.get("steps", 0)),
        cost,
        _verifier_label(run),
        reason,
    ]


HEADER = ["run_id", "task", "wall", "steps", "cost", "verifier", "termination"]


def format_line(run: RunRecord) -> str:
    return "  ".join(f"{k}={v}" for k, v in zip(HEADER, _row(run)))


def format_table(runs: list[RunRecord]) -> str:
    rows = [HEADER] + [_row(r) for r in runs]
    widths = [max(len(r[i]) for r in rows) for i in range(len(HEADER))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    verified = [r for r in runs if r.verifier_result is not None]
    passed = sum(r.verifier_result.passed for r in verified)
    cost = sum(r.estimated_cost_usd or 0.0 for r in runs)
    wall = sum(r.meta.get("wall_s", 0.0) for r in runs)
    lines.append(f"{len(runs)} runs, verifier {passed}/{len(verified)} passed, {wall:.1f}s total, ${cost:.4f} total")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    env = fake_env(config) if args.fake else real_env(config, args.windowed)
    store = TraceStore(env.config.runs_dir)
    policy = confirm_policy(args.confirm)
    if policy.note:
        print(f"confirmation: {args.confirm} -> {policy.effective} ({policy.note})", file=out)
    if args.allow_chrome and args.goal is None:
        raise RunnerError("--allow-chrome is only for freeform --goal runs; scored tasks never get Chrome")
    if args.goal is not None:
        targets: list[tuple[str | None, str | None]] = [(None, args.goal)]
    elif args.all:
        targets = [(t.id, None) for t in registry.all_tasks()]
    else:
        if args.task not in {t.id for t in registry.all_tasks()}:
            raise RunnerError(f"unknown task {args.task!r}; see `tasks`")
        targets = [(args.task, None)]
    runs: list[RunRecord] = []
    for task_id, goal in targets:
        for _ in range(args.repeat):
            run = run_task(
                task_id, goal, env.config, policy,
                device_factory=env.device_factory, inspector_factory=env.inspector_factory,
                emulator=env.emulator, store=store, model_client=env.model_client_factory(), seed=args.seed,
                settings=env.settings, allow_unblocked=args.allow_unblocked,
                lock_timeout_s=args.lock_timeout, meta={"fake": env.fake}, windowed=args.windowed,
                allow_packages=["com.android.chrome"] if args.allow_chrome else (),
            )
            runs.append(run)
            print(format_line(run), file=out)
            print(f"  replay: {store.run_dir(run.run_id) / 'replay.html'}", file=out)
    if len(runs) > 1:
        print(file=out)
        print(format_table(runs), file=out)
    return 1 if any(r.meta.get("error") for r in runs) else 0


def cmd_replay(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    print(render_replay(args.run_id, config.runs_dir), file=out)
    return 0


def cmd_ledger(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    path = Path(config.runs_dir) / "ledger.json"
    data = json.loads(path.read_text()) if path.is_file() else {"total_usd": 0.0, "entries": []}
    print(f"total ${float(data['total_usd']):.4f} over {len(data['entries'])} model calls "
          f"(budget ${config.budget_usd:.2f})", file=out)
    return 0


def cmd_tasks(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    for t in registry.all_tasks():
        print(f"{t.id:<24} flow={t.flow_type.value:<8} oracle_tier={int(t.oracle_tier)}", file=out)
    return 0


def cmd_selftest(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    from harness.verify import selftest

    rest = list(args.rest)
    if "--serial" not in rest:
        from harness.emulator import manager

        rest = ["--serial", manager.SERIAL, *rest]
    return selftest.main(rest)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m harness.cli", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a task, all tasks, or a freeform goal")
    what = run.add_mutually_exclusive_group(required=True)
    what.add_argument("--task", help="task id (see `tasks`)")
    what.add_argument("--all", action="store_true", help="every task once (times --repeat)")
    what.add_argument("--goal", help="freeform goal; no verifier, reported as unverified")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--confirm", choices=POLICIES, default="prompt")
    run.add_argument("--windowed", action="store_true", help="boot the emulator with a window")
    run.add_argument("--fake", action="store_true", help="fake device, inspector and model; no emulator or API")
    run.add_argument("--seed", type=int, help="fixed seed (same for every repeat)")
    run.add_argument("--allow-unblocked", action="store_true",
                     help="allow freeform runs when host loopback is not blocked")
    run.add_argument("--allow-chrome", action="store_true",
                     help="freeform --goal only: let the agent open com.android.chrome")
    run.add_argument("--lock-timeout", type=float, help="seconds to wait for the serial queue (default: wait)")
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", help="re-render runs/<run_id>/replay.html")
    replay.add_argument("run_id")
    replay.set_defaults(func=cmd_replay)

    sub.add_parser("ledger", help="cumulative estimated spend").set_defaults(func=cmd_ledger)
    sub.add_parser("tasks", help="list predefined tasks").set_defaults(func=cmd_tasks)

    st = sub.add_parser("selftest", help="verifier self-test (harness.verify.selftest)")
    st.add_argument("rest", nargs=argparse.REMAINDER)
    st.set_defaults(func=cmd_selftest)
    return ap


def main(argv: list[str] | None = None, out: TextIO | None = None,
         config: Config | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if getattr(args, "repeat", 1) < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    try:
        config = config or Config.from_env()
        return args.func(args, config, out)
    except (ConfigError, RunnerError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
