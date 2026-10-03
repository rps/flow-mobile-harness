"""apps-probe/run_biz.py builds its config from .env when present, else from the environment (cloud VM)."""

import importlib.util
from pathlib import Path

import pytest

from harness.emulator.profiles import business

ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch, repo_root):
    """Import run_biz with LABS_REPO_ROOT pointing at repo_root (read at import)."""
    monkeypatch.setenv("LABS_REPO_ROOT", str(repo_root))
    spec = importlib.util.spec_from_file_location("run_biz_under_test", ROOT / "apps-probe" / "run_biz.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Run:
    run_id = "r1"
    estimated_cost_usd = 0.0

    def __init__(self, error=None):
        self.meta = {"error": error} if error else {}

    class usage:
        @staticmethod
        def to_dict():
            return {}


def _capture(monkeypatch, mod, error=None):
    seen = {}

    def fake_run_scored(task, config, confirm, store, **kw):
        seen.update(task=task, config=config, confirm=confirm, kw=kw)
        return _Run(error)

    monkeypatch.setattr(business, "run_scored", fake_run_scored)
    monkeypatch.setattr(mod, "format_line", lambda run: "line")
    return seen


def test_labs_repo_root_selects_the_root(monkeypatch, tmp_path):
    assert _load(monkeypatch, tmp_path).REPO_ROOT == tmp_path


@pytest.mark.parametrize("key", [None, ""])
def test_without_env_file_and_without_a_usable_key_it_exits_before_running(monkeypatch, tmp_path, key):
    mod = _load(monkeypatch, tmp_path)
    seen = _capture(monkeypatch, mod)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    with pytest.raises(SystemExit, match="no ANTHROPIC_API_KEY"):
        mod.main(["biz_b2_rate"])
    assert seen == {}


def test_without_env_file_the_environment_supplies_key_and_budget(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    seen = _capture(monkeypatch, mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HARNESS_BUDGET_USD", "60")
    monkeypatch.setenv("HARNESS_RUNS_DIR", "/elsewhere")
    assert mod.main(["biz_b2_rate", "--confirm", "deny", "--seed", "7", "--no-window"]) == 0
    cfg = seen["config"]
    assert cfg.api_key == "sk-test" and cfg.budget_usd == 60.0
    assert Path(cfg.runs_dir) == tmp_path / "runs"  # runs always land under the repo root
    assert seen["task"] == "biz_b2_rate" and seen["confirm"] == "deny"
    assert seen["kw"]["windowed"] is False and seen["kw"]["seed"] == 7 and seen["kw"]["meta"]["worktree"] == str(ROOT)


def test_env_file_wins_and_the_shell_environment_is_not_consulted(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-file\nHARNESS_BUDGET_USD=3\nHARNESS_RUNS_DIR=/elsewhere\n")
    mod = _load(monkeypatch, tmp_path)
    seen = _capture(monkeypatch, mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    monkeypatch.setenv("HARNESS_BUDGET_USD", "60")
    mod.main(["biz_b2_rate"])
    cfg = seen["config"]
    assert cfg.api_key == "sk-from-file" and cfg.budget_usd == 3.0
    assert Path(cfg.runs_dir) == tmp_path / "runs"


def test_env_file_without_a_key_does_not_fall_back_to_the_shell(monkeypatch, tmp_path):
    """A present .env is authoritative (Mac behaviour unchanged): its missing key stays missing."""
    (tmp_path / ".env").write_text("HARNESS_BUDGET_USD=3\n")
    mod = _load(monkeypatch, tmp_path)
    seen = _capture(monkeypatch, mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    mod.main(["biz_b2_rate"])
    assert not seen["config"].api_key


def test_a_run_that_ended_in_error_returns_1(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    _capture(monkeypatch, mod, error="restore failed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert mod.main(["biz_b2_rate"]) == 1
