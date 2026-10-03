import json
from datetime import datetime

import pytest

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


@pytest.mark.parametrize("verdict, passed, false_success, false_failure", [
    (Verdict.INFEASIBLE, True, 0, 0),   # correct infeasible verdict, nothing changed (biz_h)
    (Verdict.INFEASIBLE, False, 1, 0),  # right verdict but a side effect failed the verifier
    (Verdict.DONE, True, 0, 1),         # claimed an infeasible goal done; verifier passed
    (Verdict.DONE, False, 0, 0),        # claimed done, verifier caught it: self-report agrees
    (None, True, 0, 1),                 # harness ended the run; no verdict at all
])
def test_flow_h_false_counts_use_the_infeasible_expected_verdict(tmp_path, verdict, passed,
                                                                  false_success, false_failure):
    runs = tmp_path / "runs"
    _write(runs, 1, task="h_cancel_order", flow=FlowType.H, verdict=verdict, passed=passed, fake=True)
    totals = scoreboard(runs)["fake"]["totals"]
    assert totals["false_success"] == {"count": false_success, "n": 1}
    assert totals["false_failure"] == {"count": false_failure, "n": 1}
    assert totals["agent_said_done"]["rate"] == (1.0 if verdict == Verdict.DONE else 0.0)


def test_flow_h_and_non_h_runs_in_one_group_each_use_their_own_expected_verdict(tmp_path):
    runs = tmp_path / "runs"
    _write(runs, 1, task="h_cancel_order", flow=FlowType.H, verdict=Verdict.INFEASIBLE, passed=True)
    _write(runs, 2, verdict=Verdict.INFEASIBLE, passed=True)  # flow A: infeasible is wrong -> false failure
    _write(runs, 3, verdict=Verdict.INFEASIBLE, passed=False)  # flow A: not a false success
    totals = scoreboard(runs)["real"]["totals"]
    assert totals["false_failure"] == {"count": 1, "n": 3}
    assert totals["false_success"] == {"count": 0, "n": 3}


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


def test_failure_tags_counted_per_flow_task_and_totals(tmp_path):
    from harness.scoreboard.aggregate import FAILURE_TAGS, load_tags, save_tags

    runs = tmp_path / "runs"
    _write(runs, 1)
    _write(runs, 2)
    _write(runs, 3, task="f_send_sms", flow=FlowType.F)
    save_tags(runs / "20260101-000001-aaaaaa", ["lost_state", "wrong_element"])
    save_tags(runs / "20260101-000002-aaaaaa", ["lost_state"])
    (runs / "20260101-000003-aaaaaa" / "tags.json").write_text("{broken")  # unreadable: no tags
    board = scoreboard(runs)["real"]
    a = next(r for r in board["by_flow"] if r["flow_type"] == "a")
    assert a["failure_tags"]["lost_state"] == 2 and a["failure_tags"]["wrong_element"] == 1
    assert set(a["failure_tags"]) == set(FAILURE_TAGS)
    f = next(r for r in board["by_task"] if r["task_id"] == "f_send_sms")
    assert sum(f["failure_tags"].values()) == 0
    assert board["totals"]["failure_tags"]["lost_state"] == 2
    assert load_tags(runs / "20260101-000001-aaaaaa")["tags"] == ["wrong_element", "lost_state"]  # canonical order


def test_save_tags_rejects_unknown_and_drops_duplicates(tmp_path):
    from harness.scoreboard.aggregate import load_tags, save_tags

    with pytest.raises(ValueError, match="unknown tags"):
        save_tags(tmp_path, ["lost_state", "bogus"])
    assert not (tmp_path / "tags.json").exists()
    out = save_tags(tmp_path, ["login_wall", "login_wall"])
    assert out["tags"] == ["login_wall"] and out["updated_at"]
    assert load_tags(tmp_path)["tags"] == ["login_wall"]
    datetime.fromisoformat(out["updated_at"])  # a real timestamp
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tags.json"]  # no .tmp left behind
    (tmp_path / "tags.json").write_text(json.dumps({"tags": ["login_wall", "made_up"]}))
    assert load_tags(tmp_path)["tags"] == ["login_wall"]


@pytest.mark.parametrize("raw", ['["login_wall"]', '{"tags": 5}', '{"tags": "login_wall"}', "null"])
def test_load_tags_wrong_shape_reads_as_no_tags(tmp_path, raw):
    from harness.scoreboard.aggregate import load_tags

    (tmp_path / "tags.json").write_text(raw)
    assert load_tags(tmp_path) == {"tags": [], "updated_at": None}


def test_fake_run_tags_do_not_count_in_real_group(tmp_path):
    from harness.scoreboard.aggregate import save_tags

    runs = tmp_path / "runs"
    _write(runs, 1, fake=True)
    _write(runs, 2)
    save_tags(runs / "20260101-000001-aaaaaa", ["false_success"])
    board = scoreboard(runs)
    assert board["fake"]["totals"]["failure_tags"]["false_success"] == 1
    assert board["real"]["totals"]["failure_tags"]["false_success"] == 0


def test_oracle_tier_falls_back_to_registry_when_unverified(tmp_path):
    runs = tmp_path / "runs"
    _write(runs, 1, task="f_send_sms", flow=FlowType.F, passed=None)  # errored before verify
    _write(runs, 2, task="gone_task", passed=None)
    _write(runs, 3, task=None, flow=FlowType.FREEFORM, passed=None)
    tasks = {r["task_id"]: r for r in scoreboard(runs)["real"]["by_task"]}
    assert tasks["f_send_sms"]["oracle_tier"] == int(OracleTier.DOWNSTREAM_EFFECT) == 3  # f_send_sms is tier 3
    assert tasks["gone_task"]["oracle_tier"] is None
    assert tasks["freeform"]["oracle_tier"] is None


def test_oracle_tier_from_a_verified_run_beats_the_registry(tmp_path):
    runs = tmp_path / "runs"
    _write(runs, 1, task="f_send_sms", flow=FlowType.F, passed=None)
    _write(runs, 2, task="f_send_sms", flow=FlowType.F, passed=True)  # _write records OWN_STORAGE (1)
    tasks = {r["task_id"]: r for r in scoreboard(runs)["real"]["by_task"]}
    assert tasks["f_send_sms"]["oracle_tier"] == int(OracleTier.OWN_STORAGE) == 1
