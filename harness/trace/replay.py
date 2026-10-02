"""Render one run as a self-contained HTML file: runs/<run_id>/replay.html.

Screenshots are inlined as base64; no scripts and no external assets, so a
tester can open the file with nothing else installed.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from harness.contracts import CheckResult, RunRecord, StepRecord, TokenUsage
from harness.trace.store import TraceStore

_CSS = """
:root { --bg: #fafafa; --fg: #1a1a1a; --muted: #666; --card: #fff; --line: #ddd;
  --pass: #1a7f37; --fail: #c62828; --code: #f0f0f0; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #121212; --fg: #e6e6e6; --muted: #9a9a9a; --card: #1e1e1e; --line: #333;
    --pass: #4cc26a; --fail: #ef6b6b; --code: #2a2a2a; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.3rem; margin: 0 0 12px; overflow-wrap: anywhere; }
h2 { font-size: 1.1rem; margin: 24px 0 8px; }
h3 { font-size: 1rem; margin: 0 0 8px; }
section, article { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px; margin: 0 0 12px; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 12px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; overflow-wrap: anywhere; min-width: 0; }
img { display: block; max-width: 100%; max-height: 80vh; height: auto;
  border: 1px solid var(--line); border-radius: 4px; margin: 0 0 8px; }
pre { background: var(--code); padding: 8px; border-radius: 4px; margin: 0;
  white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.muted { color: var(--muted); }
.pass { color: var(--pass); font-weight: 600; }
.fail { color: var(--fail); font-weight: 600; }
ul.checks { list-style: none; padding: 0; margin: 0 0 8px; }
ul.checks li { padding: 2px 0; overflow-wrap: anywhere; }
"""


def _e(value: Any) -> str:
    return escape(str(value))


def _or(value: Any, placeholder: str) -> str:
    """Escaped value, or a muted placeholder when it is None or empty."""
    if value is None or value == "":
        return f'<span class="muted">{_e(placeholder)}</span>'
    return _e(value)


def _json(value: Any) -> str:
    return _e(json.dumps(value, indent=2, ensure_ascii=False))


def _duration(started_at: str, ended_at: str | None) -> str:
    if not ended_at:
        return '<span class="muted">unfinished</span>'
    try:
        secs = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds()
    except (ValueError, TypeError):
        return '<span class="muted">unknown</span>'
    return f"{secs:.1f} s"


def _usage(u: TokenUsage) -> str:
    return _e(
        f"{u.input_tokens} in, {u.output_tokens} out, "
        f"{u.cache_read_input_tokens} cache read, {u.cache_creation_input_tokens} cache write"
    )


def _dl(rows: list[tuple[str, str]]) -> str:
    return "<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def _passfail(passed: bool) -> str:
    return '<span class="pass">PASS</span>' if passed else '<span class="fail">FAIL</span>'


def _image(run_dir: Path, rel: str | None) -> str:
    if not rel:
        return '<p class="muted">screenshot not recorded</p>'
    path = (run_dir / rel).resolve()
    if not path.is_relative_to(run_dir.resolve()) or not path.is_file():
        return f'<p class="muted">screenshot not recorded ({_e(rel)})</p>'
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}" alt="{_e(rel)}">'


def _step(run_dir: Path, s: StepRecord) -> str:
    return (
        f'<article id="step-{s.index}"><h3>Step {s.index}: {_e(s.tool_name)}</h3>'
        + _image(run_dir, s.screenshot_path)
        + _dl([
            ("Reasoning", _or(s.reasoning, "none given")),
            ("Input", f"<pre>{_json(s.tool_input)}</pre>"),
            ("Result", f"<pre>{_json(s.tool_result)}</pre>"),
            ("Duration", f"{s.duration_ms} ms"),
            ("UI tree", _or(s.ui_tree_path, "not recorded")),
            ("Tokens", _usage(s.usage)),
        ])
        + "</article>"
    )


def _checks(title: str, checks: list[CheckResult]) -> str:
    if not checks:
        return f'<h3>{_e(title)}</h3><p class="muted">none</p>'
    items = "".join(
        f"<li>{_passfail(c.passed)} {_e(c.name)}"
        + (f' <span class="muted">{_e(c.detail)}</span>' if c.detail else "")
        + "</li>"
        for c in checks
    )
    return f'<h3>{_e(title)}</h3><ul class="checks">{items}</ul>'


def _verifier(run: RunRecord) -> str:
    v = run.verifier_result
    if v is None:
        return '<section><h2>Verifier</h2><p class="muted">no verifier (freeform)</p></section>'
    agrees = {True: "yes", False: "no", None: "not compared"}[v.self_report_agrees]
    return (
        "<section><h2>Verifier</h2>"
        + _dl([
            ("Result", _passfail(v.passed)),
            ("Oracle tier", _e(f"{int(v.oracle_tier)} ({v.oracle_tier.name.lower()})")),
            ("Self-report agrees", _e(agrees)),
        ])
        + _checks("End state", v.end_state)
        + _checks("Side effects", v.side_effects)
        + _checks("Process", v.process)
        + "</section>"
    )


def _page(run_dir: Path, run: RunRecord, steps: list[StepRecord]) -> str:
    cost = f"${run.estimated_cost_usd:.4f}" if run.estimated_cost_usd is not None else None
    header = _dl([
        ("Run", _e(run.run_id)),
        ("Task", _or(run.task_id, "none")),
        ("Flow type", _e(run.flow_type.value)),
        ("Model", _e(run.model)),
        ("Started", _e(run.started_at)),
        ("Ended", _or(run.ended_at, "unfinished")),
        ("Duration", _duration(run.started_at, run.ended_at)),
        ("Steps", _e(len(steps))),
        ("Tokens", _usage(run.usage)),
        ("Estimated cost", _or(cost, "unknown")),
        ("Seed", _or(run.seed, "not recorded")),
        ("Runner", _or(json.dumps(run.meta, sort_keys=True) if run.meta else None, "none")),
    ])
    verdict = run.agent_verdict.value if run.agent_verdict else None
    reason = run.termination_reason.value if run.termination_reason else None
    outcome = _dl([
        ("Verdict", _or(verdict, "none (harness ended the run)")),
        ("Summary", _or(run.agent_summary, "none")),
        ("Termination", _or(reason, "unfinished")),
    ])
    body = "".join(_step(run_dir, s) for s in steps) or '<p class="muted">no steps recorded</p>'
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Run {_e(run.run_id)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{_e(run.goal)}</h1><section>{header}</section>"
        f"<h2>Steps</h2>{body}"
        f"<section><h2>Agent self-report</h2>{outcome}</section>"
        f"{_verifier(run)}"
        "</main></body></html>\n"
    )


def render_replay(run_id: str, runs_dir: str | os.PathLike[str] = "runs") -> Path:
    """Write runs/<run_id>/replay.html and return its path."""
    store = TraceStore(runs_dir)
    run, steps = store.load_run(run_id)
    run_dir = store.run_dir(run_id)
    out = run_dir / "replay.html"
    out.write_text(_page(run_dir, run, steps), encoding="utf-8")
    return out
