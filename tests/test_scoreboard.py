import json

from harness.contracts import (
    FlowType,
    OracleTier,
    RunRecord,
    TerminationReason,
    Verdict,
    VerifierResult,
)
from harness.scoreboard.aggregate import load_runs, scoreboard


def _write(runs_dir, n, *, task="a_markor_note", flow=FlowType.A, verdict=Verdict.DONE, passed=True,
           fake=False, cost=0.1, steps=4, wall=10.0):
    run = RunRecord(
        run_id=f"20260101-0000{n:02d}-aaaaaa", task_id=task, flow_type=flow, goal="g", model="m",
        started_at="2026-01-01T00:00:00+00:00", ended_at="2026-01-01T00:00:10+00:00", agent_verdict=verdict,
        termination_reason=TerminationReason.FINISHED, estimated_cost_usd=cost,
        verifier_result=None if passed is None else VerifierResult(passed=passed, oracle_tier=OracleTier.OWN_STORAGE),
        meta={"fake": fake, "steps": steps, "wall_s": wall},
    )
    d = runs_dir / run.run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(run.to_dict()))


def test_rates_counts_and_means_per_flow_and_task(tmp_path):
    runs = tmp_path / "runs"
    _write(runs, 1, verdict=Verdict.DONE, passed=True, cost=0.1, steps=4, wall=10)
    _write(runs, 2, verdict=Verdict.DONE, passed=False, cost=0.3, steps=8, wall=20)   # false success
    _write(runs, 3, verdict=Verdict.FAILED, passed=True, cost=None, steps=6, wall=30)  # false failure
    _write(runs, 4, task="f_send_sms", flow=FlowType.F, verdict=None, passed=False)
    (runs / "ledger.json").write_text(json.dumps({"total_usd": 1.25, "entries": []}))

    board = scoreboard(runs)
    a = next(r for r in board["real"]["by_flow"] if r["flow_type"] == "a")
    assert a["runs"] == 3
    assert a["verifier_pass"] == {"rate": 2 / 3, "n": 3}
    assert a["agent_said_done"] == {"rate": 2 / 3, "n": 3}
    assert a["false_success"] == {"count": 1, "n": 3}
    assert a["false_failure"] == {"count": 1, "n": 3}
    assert a["steps"] == {"mean": 6.0, "n": 3}
    assert a["cost_usd"]["n"] == 2 and abs(a["cost_usd"]["mean"] - 0.2) < 1e-9  # unpriced run excluded
    assert a["wall_s"] == {"mean": 20.0, "n": 3}
    f = next(r for r in board["real"]["by_flow"] if r["flow_type"] == "f")
    assert f["agent_said_done"] == {"rate": 0.0, "n": 1} and f["false_failure"]["count"] == 0
    tasks = {r["task_id"]: r for r in board["real"]["by_task"]}
    assert tasks["a_markor_note"]["runs"] == 3 and tasks["a_markor_note"]["oracle_tier"] == 1
    assert board["real"]["totals"]["runs"] == 4
    assert board["ledger_total_usd"] == 1.25
    assert board["fake"]["totals"]["runs"] == 0


def test_fake_runs_are_separate_and_freeform_excluded_from_verifier_rates(tmp_path):
    runs = tmp_path / "runs"
    _write(runs, 1, fake=True, passed=False)
    _write(runs, 2, task=None, flow=FlowType.FREEFORM, passed=None)
    board = scoreboard(runs)
    assert board["fake"]["totals"]["runs"] == 1 and board["real"]["totals"]["runs"] == 1
    ff = board["real"]["by_flow"][0]
    assert ff["flow_type"] == "freeform" and ff["verified_runs"] == 0
    assert ff["verifier_pass"] == {"rate": None, "n": 0}
    assert board["real"]["by_task"][0]["task_id"] == "freeform"


def test_empty_or_missing_dir_and_unreadable_files(tmp_path):
    assert scoreboard(tmp_path / "nope")["real"]["totals"]["runs"] == 0
    runs = tmp_path / "runs"
    _write(runs, 1)
    bad = runs / "20260101-000099-bbbbbb"
    bad.mkdir()
    (bad / "run.json").write_text("{not json")
    (runs / "ledger.json").write_text("garbage")
    assert len(load_runs(runs)) == 1
    assert scoreboard(runs)["ledger_total_usd"] == 0.0
