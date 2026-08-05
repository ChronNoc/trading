"""Tests for fail-closed paper-engine configuration readers."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.paper.options import read_fixed_sizing


_DISABLED = {
    "fixed_contracts": 0,
    "max_risk_per_trade_usd": Decimal("0"),
}


def test_fixed_sizing_reader_loads_a_complete_valid_pair(tmp_path: Path) -> None:
    config = tmp_path / "production.yaml"
    config.write_text(
        "paper_fixed_contracts: 4\npaper_max_risk_per_trade_usd: 80\n",
        encoding="utf-8",
    )

    assert read_fixed_sizing(config) == {
        "fixed_contracts": 4,
        "max_risk_per_trade_usd": Decimal("80"),
    }


@pytest.mark.parametrize(
    "payload",
    [
        "paper_fixed_contracts: 4\n",
        "paper_max_risk_per_trade_usd: 80\n",
        "paper_fixed_contracts: invalid\npaper_max_risk_per_trade_usd: 80\n",
        "paper_fixed_contracts: 4\npaper_max_risk_per_trade_usd: invalid\n",
        "paper_fixed_contracts: 0\npaper_max_risk_per_trade_usd: 80\n",
        "paper_fixed_contracts: 4\npaper_max_risk_per_trade_usd: 0\n",
        "paper_fixed_contracts: true\npaper_max_risk_per_trade_usd: 80\n",
        "paper_fixed_contracts: 4\npaper_max_risk_per_trade_usd: .inf\n",
    ],
)
def test_fixed_sizing_reader_fails_closed_for_incomplete_or_invalid_pairs(
    tmp_path: Path,
    payload: str,
) -> None:
    config = tmp_path / "production.yaml"
    config.write_text(payload, encoding="utf-8")

    assert read_fixed_sizing(config) == _DISABLED


def test_fixed_sizing_reader_fails_closed_for_missing_or_unreadable_config(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.yaml"
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("paper_fixed_contracts: [", encoding="utf-8")

    assert read_fixed_sizing(missing) == _DISABLED
    assert read_fixed_sizing(malformed) == _DISABLED


def test_min_reward_risk_reader_is_fail_closed(tmp_path: Path) -> None:
    from app.paper.options import read_min_reward_risk

    assert read_min_reward_risk(tmp_path / "missing.yaml") == Decimal("0")
    good = tmp_path / "good.yaml"
    good.write_text("paper_min_reward_risk: 1.5\n", encoding="utf-8")
    assert read_min_reward_risk(good) == Decimal("1.5")
    # Every invalid form disables the filter (0), never blocks trading by accident.
    for bad in ("paper_min_reward_risk: -1\n", "paper_min_reward_risk: abc\n",
                "paper_min_reward_risk: true\n", "other_key: 1\n", "paper_min_reward_risk: [\n"):
        path = tmp_path / "bad.yaml"
        path.write_text(bad, encoding="utf-8")
        assert read_min_reward_risk(path) == Decimal("0"), bad
