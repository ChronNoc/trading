"""Strategy profile registry and fallback selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.market.regime_classifier import BehaviorRegime, LiquidityRegime, VolatilityRegime

ANY_VALUE = "any"
OBSERVE_ONLY_PROFILE_ID = "observe_only"
DEFAULT_PROFILE_CONFIG = Path("config/session_profiles.yaml")


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    """One routed strategy profile."""

    profile_id: str
    session: str
    volatility: str = ANY_VALUE
    liquidity: str = ANY_VALUE
    behavior: str = ANY_VALUE
    strategy_version: str = "rules-v0"
    model_version: str | None = None
    threshold_rules: dict[str, dict[str, object]] | None = None
    minimum_historical_sample_size: int = 0
    historical_sample_count: int = 0
    validation_status: str = "provisional"
    decisions: str = "shadow"

    @property
    def decisions_allowed(self) -> bool:
        """Return whether this profile allows shadow decisions."""
        return self.decisions == "shadow" and self.validation_status in {"validated", "provisional"}


@dataclass(frozen=True, slots=True)
class ProfileSelection:
    """Selected profile plus fallback metadata for display and logging."""

    profile: StrategyProfile
    fallback_level: str
    fallback_reason: str
    decisions_allowed: bool


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    """Load and select session/regime-specific strategy profiles."""

    profiles: tuple[StrategyProfile, ...]

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_PROFILE_CONFIG) -> "ProfileRegistry":
        """Load profiles from YAML."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("profile config must contain a mapping")
        profiles = tuple(_profile_from_mapping(item) for item in raw.get("profiles", ()))
        if not profiles:
            profiles = (observe_only_profile(),)
        return cls(profiles=profiles)

    def select(
        self,
        *,
        session_name: str,
        volatility: VolatilityRegime | str,
        liquidity: LiquidityRegime | str,
        behavior: BehaviorRegime | str,
    ) -> ProfileSelection:
        """Select the most specific matching profile with deterministic fallback."""
        values = {
            "session": session_name,
            "volatility": str(volatility),
            "liquidity": str(liquidity),
            "behavior": str(behavior),
        }
        exact = self._find(values)
        if exact is not None:
            return _selection(exact, "exact", "matched session, volatility, liquidity, and behavior")

        session_default = self._find(
            {
                "session": session_name,
                "volatility": ANY_VALUE,
                "liquidity": ANY_VALUE,
                "behavior": ANY_VALUE,
            },
        )
        if session_default is not None:
            return _selection(session_default, "session", "fell back to the session default profile")

        global_default = self._find(
            {
                "session": "global",
                "volatility": ANY_VALUE,
                "liquidity": ANY_VALUE,
                "behavior": ANY_VALUE,
            },
        )
        if global_default is not None:
            return _selection(global_default, "global", "fell back to the global profile")

        profile = observe_only_profile()
        return _selection(profile, "observe_only", "no matching profile is available")

    def _find(self, values: dict[str, str]) -> StrategyProfile | None:
        for profile in self.profiles:
            if (
                profile.session == values["session"]
                and profile.volatility == values["volatility"]
                and profile.liquidity == values["liquidity"]
                and profile.behavior == values["behavior"]
            ):
                return profile
        return None


def observe_only_profile() -> StrategyProfile:
    """Return a profile that always records and blocks decisions."""
    return StrategyProfile(
        profile_id=OBSERVE_ONLY_PROFILE_ID,
        session="global",
        decisions="observe_only",
        validation_status="unavailable",
        historical_sample_count=0,
    )


def _profile_from_mapping(item: Any) -> StrategyProfile:
    if not isinstance(item, dict):
        raise ValueError("profile entries must be mappings")
    threshold_rules = item.get("threshold_rules")
    if threshold_rules is not None and not isinstance(threshold_rules, dict):
        raise ValueError("threshold_rules must be a mapping")
    return StrategyProfile(
        profile_id=str(item["id"]),
        session=str(item.get("session", "global")),
        volatility=str(item.get("volatility", ANY_VALUE)),
        liquidity=str(item.get("liquidity", ANY_VALUE)),
        behavior=str(item.get("behavior", ANY_VALUE)),
        strategy_version=str(item.get("strategy_version", "rules-v0")),
        model_version=None if item.get("model_version") is None else str(item["model_version"]),
        threshold_rules=threshold_rules,
        minimum_historical_sample_size=int(item.get("minimum_historical_sample_size", 0)),
        historical_sample_count=int(item.get("historical_sample_count", 0)),
        validation_status=str(item.get("validation_status", "provisional")),
        decisions=str(item.get("decisions", "shadow")),
    )


def _selection(profile: StrategyProfile, fallback_level: str, reason: str) -> ProfileSelection:
    enough_samples = profile.historical_sample_count >= profile.minimum_historical_sample_size
    decisions_allowed = profile.decisions_allowed and enough_samples
    if not enough_samples and profile.decisions == "shadow":
        reason = f"{reason}; historical sample count below minimum"
    return ProfileSelection(
        profile=profile,
        fallback_level=fallback_level,
        fallback_reason=reason,
        decisions_allowed=decisions_allowed,
    )

