import json
import logging

import pytest

from harness.agent.pricing import PRICING_PATH, Ledger, estimate_cost, load_prices
from harness.contracts import TokenUsage


def test_price_table_records_source_and_date():
    data = json.loads(PRICING_PATH.read_text())
    assert data["source_url"].startswith("https://platform.claude.com/")
    assert data["retrieved"]
    for price in data["models"].values():
        assert set(price) == {"input", "output", "cache_read", "cache_write_5m"}


def test_estimate_cost_opus():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=500_000,
                       cache_creation_input_tokens=200_000)
    assert estimate_cost("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0 + 0.1 + 1.0)


def test_unknown_model_cost_none_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert estimate_cost("claude-nope", TokenUsage(input_tokens=5)) is None
    assert "claude-nope" in caplog.text


def test_ledger_persists_and_starts_empty(tmp_path):
    path = tmp_path / "sub" / "ledger.json"
    assert Ledger(path).total() == 0.0
    assert Ledger(path).add(0.25, "run-a") == pytest.approx(0.25)
    assert Ledger(path).add(0.5, "run-b") == pytest.approx(0.75)
    fresh = Ledger(path)
    assert fresh.total() == pytest.approx(0.75)
    entries = json.loads(path.read_text())["entries"]
    assert [e["run"] for e in entries] == ["run-a", "run-b"]


def test_load_prices_has_default_model():
    assert "claude-opus-5-5" in load_prices()["models"]
