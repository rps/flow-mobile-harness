"""Per-run trace folder: runs/<run_id>/run.json, steps.jsonl, step_NNN.png/.txt.

Steps are appended one JSON line at a time and fsynced, and run.json is
replaced atomically, so a crash mid-run leaves a readable partial trace.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import RunRecord, StepRecord

log = logging.getLogger(__name__)

RUN_FILE = "run.json"
STEPS_FILE = "steps.jsonl"


def new_run_id() -> str:
    """`YYYYMMDD-HHMMSS-<6 hex>`, UTC."""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _run_json(run: RunRecord) -> bytes:
    return json.dumps(run.to_dict(), indent=2).encode()


class RunWriter:
    """Writes one run's steps and final record. Get one from TraceStore.start_run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def write_step(
        self, step: StepRecord, screenshot_png: bytes | None, ui_tree: str | None
    ) -> StepRecord:
        """Save the step's files, then append its record. Returns the stored record."""
        stem = f"step_{step.index:03d}"
        if screenshot_png is not None:
            _write_atomic(self.run_dir / f"{stem}.png", screenshot_png)
            step = dataclasses.replace(step, screenshot_path=f"{stem}.png")
        if ui_tree is not None:
            _write_atomic(self.run_dir / f"{stem}.txt", ui_tree.encode())
            step = dataclasses.replace(step, ui_tree_path=f"{stem}.txt")
        line = json.dumps(step.to_dict()) + "\n"
        with open(self.run_dir / STEPS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return step

    def finish(self, run: RunRecord) -> None:
        _write_atomic(self.run_dir / RUN_FILE, _run_json(run))


class TraceStore:
    """Run folders under runs_dir.

    After load_run, `last_skipped_lines` holds how many unreadable lines in
    steps.jsonl were skipped (each is also logged as a warning).
    """

    def __init__(self, runs_dir: str | os.PathLike[str]) -> None:
        self.runs_dir = Path(runs_dir)
        self.last_skipped_lines = 0

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def start_run(self, run: RunRecord) -> RunWriter:
        """Create the run folder and write the initial run.json.

        Raises FileExistsError if the folder already holds a run.json.
        """
        run_dir = self.run_dir(run.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if (run_dir / RUN_FILE).exists():
            raise FileExistsError(f"run {run.run_id} already exists in {self.runs_dir}")
        _write_atomic(run_dir / RUN_FILE, _run_json(run))
        return RunWriter(run_dir)

    def list_runs(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.name for p in self.runs_dir.iterdir() if (p / RUN_FILE).is_file())

    def load_run(self, run_id: str) -> tuple[RunRecord, list[StepRecord]]:
        """Load a run, finished or not. Unreadable step lines are skipped."""
        run_dir = self.run_dir(run_id)
        run = RunRecord.from_dict(json.loads((run_dir / RUN_FILE).read_text()))
        steps: list[StepRecord] = []
        skipped = 0
        steps_path = run_dir / STEPS_FILE
        if steps_path.is_file():
            with open(steps_path, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        steps.append(StepRecord.from_dict(json.loads(line)))
                    except (ValueError, KeyError, TypeError) as e:
                        skipped += 1
                        log.warning("%s line %d skipped: %s", steps_path, lineno, e)
        self.last_skipped_lines = skipped
        return run, steps
