"""Tests for the typed Lucid Flex 25K profile and its enforcement."""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.risk.account_profile import (
    DrawdownMethod,
    NewsPolicy,
    ProjectSafetyLimits,
    lucid_flex_25k,
    load_selected_profile,
)


def test_lucid_flex_25k_matches_the_verified_official_rules() -> None:
    """Every value is the one read from Lucid's own pages on 2026-07-17."""
    p = lucid_flex_25k()
    assert p.account_size == Decimal("25000")
    assert p.profit_target == Decimal("1250")
    assert p.max_loss_limit == Decimal("1000")
    assert p.drawdown_method is DrawdownMethod.EOD_TRAILING_THEN_LOCK
    assert p.initial_trail_balance == Decimal("26100")
    assert p.locked_mll_balance == Decimal("25100")
    assert p.max_micro_contracts == 20 and p.max_mini_contracts == 2
    assert p.consistency_max_day_share == Decimal("0.50")
    assert p.consistency_applies_to_funded is False
    assert p.daily_loss_limit is None  # Lucid imposes no DLL on Flex evals
    assert p.session_close == time(16, 45, tzinfo=p.session_close.tzinfo)
    assert p.session_close.tzinfo is not None  # timezone-aware, America/New_York
    assert p.news_policy is NewsPolicy.UNRESTRICTED
    assert p.flatten_overnight is True
    assert p.min_hold_seconds_for_profit_share == 5
    assert any("microscalping" in b for b in p.prohibited_behaviours)


def test_profile_is_resolved_and_carries_provenance() -> None:
    """A verified profile records its source URL, dates, and an excerpt hash."""
    p = lucid_flex_25k()
    assert p.resolved is True
    assert p.blocks_broker_arming is False
    assert p.provenance.verified is True
    assert "lucidtrading.com" in p.provenance.source_url
    assert p.provenance.retrieval_date == "2026-07-17"
    assert p.provenance.effective_date == "2026-04-15"
    assert len(p.provenance.excerpt_sha256) == 64  # detects a silent upstream edit


def test_eod_trailing_threshold_then_locks() -> None:
    """MLL trails the highest CLOSING balance, then locks past the trail balance."""
    p = lucid_flex_25k()
    # At the start: threshold is account - MLL = 24,000.
    assert p.trailing_threshold(Decimal("25000")) == Decimal("24000")
    # Trails up with the closing balance: 25,800 - 1,000 = 24,800.
    assert p.trailing_threshold(Decimal("25800")) == Decimal("24800")
    # Past the initial trail balance (26,100) it LOCKS at 25,100 forever.
    assert p.trailing_threshold(Decimal("26200")) == Decimal("25100")
    assert p.trailing_threshold(Decimal("40000")) == Decimal("25100")
    # It never trails below the starting floor.
    assert p.trailing_threshold(Decimal("24000")) == Decimal("24000")


def test_drawdown_room_and_target_progress() -> None:
    p = lucid_flex_25k()
    assert p.drawdown_room(Decimal("25000"), Decimal("25000")) == Decimal("1000")
    assert p.drawdown_room(Decimal("24500"), Decimal("25000")) == Decimal("500")
    # Breach point: room hits zero at the threshold.
    assert p.drawdown_room(Decimal("24000"), Decimal("25000")) == Decimal("0")
    assert p.target_progress(Decimal("25000")) == Decimal("0")
    assert p.target_progress(Decimal("25625")) == Decimal("0.5")  # half of $1,250
    assert p.target_progress(Decimal("26250")) == Decimal("1")
    assert p.target_progress(Decimal("30000")) == Decimal("1")  # capped


def test_consistency_rule_uses_50_percent_day_share() -> None:
    p = lucid_flex_25k()
    # One day supplying 60% of profit breaches the 50% rule.
    assert p.consistency_breached({"d1": Decimal("600"), "d2": Decimal("400")}) is True
    assert p.consistency_breached({"d1": Decimal("500"), "d2": Decimal("500")}) is False
    assert p.consistency_breached({}) is False  # no profit yet -> not a breach


def test_project_safety_limits_are_stricter_and_cannot_be_widened() -> None:
    """Lucid has no DLL, but the project's own locks still bind."""
    p = lucid_flex_25k()
    limits = p.effective_limits()
    assert limits.max_contracts == 20  # the Lucid micro cap
    assert limits.max_entries_per_day == 3  # project rule, stricter than "unlimited"
    assert limits.max_losses_per_day == 3
    assert limits.allow_averaging_down is False
    assert limits.daily_loss_cap is None  # firm imposes none; project risk budget binds
    with pytest.raises(ValueError):
        ProjectSafetyLimits(allow_averaging_down=True)  # never permitted
    with pytest.raises(ValueError):
        ProjectSafetyLimits(max_entries_per_day=0)


def test_selected_profile_defaults_to_lucid_not_a_fabricated_100k() -> None:
    """The default account is the verified Lucid 25K - never an invented $100k."""
    profile = load_selected_profile()
    assert profile.profile_id == "lucid_flex_25k"
    assert profile.account_size == Decimal("25000")


def test_unverified_yaml_blocks_arming_but_keeps_the_typed_shape() -> None:
    """An unresolved profile blocks DEMO/LIVE but never recording/paper research."""
    profile = load_selected_profile(Path("config/prop_rules_lucid.yaml"))
    assert profile.blocks_broker_arming is True
    assert profile.unresolved_fields
    assert profile.account_size > 0  # still usable for paper/recording


def test_verified_flex_yaml_resolves() -> None:
    profile = load_selected_profile(Path("config/prop_profiles/lucid_flex_25k.yaml"))
    assert profile.resolved is True
    assert profile.blocks_broker_arming is False


def test_paper_ledger_config_comes_from_the_profile_not_a_hardcoded_100k() -> None:
    """The canonical paper account uses the selected profile's real numbers."""
    from app.research.paper_ledger import RealPaperLedgerConfig

    config = RealPaperLedgerConfig.from_profile()
    assert config.starting_balance == Decimal("25000")
    assert config.remaining_allowable_drawdown == Decimal("1000")
    assert config.max_contracts == 20
    assert config.profile_id == "lucid_flex_25k"
    # The old fabricated default must be gone from the dataclass defaults too.
    assert RealPaperLedgerConfig().starting_balance == Decimal("25000")
