"""Route sessions and regimes to strategy profiles."""

from __future__ import annotations

from dataclasses import dataclass

from app.market.regime_classifier import RegimeClassification
from app.market.session_context import SessionContext
from app.strategy.profile_registry import ProfileRegistry, ProfileSelection


@dataclass(frozen=True, slots=True)
class SessionRoutingResult:
    """Profile routing result with decision gating metadata."""

    selection: ProfileSelection
    decisions_allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SessionRouter:
    """Select strategy profiles from session and regime context."""

    profile_registry: ProfileRegistry

    def route(self, session: SessionContext, regime: RegimeClassification) -> SessionRoutingResult:
        """Return the selected profile for a session/regime pair."""
        if not session.is_open:
            selection = self.profile_registry.select(
                session_name="closed",
                volatility=regime.volatility,
                liquidity=regime.liquidity,
                behavior=regime.behavior,
            )
            return SessionRoutingResult(
                selection=selection,
                decisions_allowed=False,
                reason="market session is closed",
            )

        selection = self.profile_registry.select(
            session_name=session.name,
            volatility=regime.volatility,
            liquidity=regime.liquidity,
            behavior=regime.behavior,
        )
        if not regime.decisions_allowed:
            return SessionRoutingResult(
                selection=selection,
                decisions_allowed=False,
                reason=f"regime blocks decisions: {regime.reason}",
            )
        if not selection.decisions_allowed:
            return SessionRoutingResult(
                selection=selection,
                decisions_allowed=False,
                reason=selection.fallback_reason,
            )
        return SessionRoutingResult(
            selection=selection,
            decisions_allowed=True,
            reason="profile allows shadow decisions",
        )

