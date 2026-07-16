"""The future LIVE-trading gate: every requirement explicit, no bypass possible.

``evaluate_live_gate`` re-checks EVERY requirement on EVERY call and returns the
complete list of failures. There is no cached pass, no override flag, and no
partial-credit path. Arming state lives only in process memory
(:class:`ArmingState`), so every application startup is disarmed by
construction. ``live_enabled`` ships false and generated code never sets it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path

LIVE_CONFIRMATION_PHRASE = "ARM LIVE TRADING"


@dataclass(frozen=True, slots=True)
class LiveGateInputs:
    """Every input the LIVE gate evaluates. All default to the FAILING value."""

    live_enabled_in_config: bool = False
    live_credentials_present: bool = False
    strategy_version_validated: bool = False
    deterministic_setup_accepted: bool = False
    risk_approval_allowed: bool = False
    completed_sample_sufficient: bool = False
    independent_days_sufficient: bool = False
    expectancy_positive_after_costs: bool = False
    drawdown_within_limit: bool = False
    loss_streak_within_limit: bool = False
    calibration_gate_passed: bool = False
    drift_gate_passed: bool = False
    feed_health_clean: bool = False
    demo_soak_completed: bool = False
    account_state_synchronized: bool = False
    contract_month_correct: bool = False
    no_pending_cancellation: bool = False
    kill_switch_healthy: bool = False
    prop_rules_resolved: bool = False
    typed_confirmation_phrase: str = ""
    second_confirmation_acknowledged: bool = False


_REQUIREMENT_LABELS: dict[str, str] = {
    "live_enabled_in_config": "live_enabled is not true in the production configuration",
    "live_credentials_present": "no valid Tradovate LIVE credentials are present",
    "strategy_version_validated": "the strategy/model version has not passed validation",
    "deterministic_setup_accepted": "no deterministic setup is currently accepted",
    "risk_approval_allowed": "the risk engine has not issued an approval",
    "completed_sample_sufficient": "the completed out-of-sample setup sample is insufficient",
    "independent_days_sufficient": "the independent trading-day minimum is not met",
    "expectancy_positive_after_costs": "net expectancy after costs is not positive",
    "drawdown_within_limit": "drawdown exceeds the configured limit",
    "loss_streak_within_limit": "the consecutive-loss streak exceeds the limit",
    "calibration_gate_passed": "the calibration gate has not passed",
    "drift_gate_passed": "the drift gate has not passed",
    "feed_health_clean": "recent feed health is not clean",
    "demo_soak_completed": "the Tradovate DEMO soak period has not completed successfully",
    "account_state_synchronized": "the broker account state is not synchronized",
    "contract_month_correct": "the contract month is not verified as current",
    "no_pending_cancellation": "an order cancellation is still pending",
    "kill_switch_healthy": "the kill switch is not healthy",
    "prop_rules_resolved": "the prop-firm rule profile is unresolved/unverified",
}


@dataclass(frozen=True, slots=True)
class LiveGateDecision:
    """The complete gate outcome: allowed only when there are zero failures."""

    allowed: bool
    failures: tuple[str, ...]


def evaluate_live_gate(inputs: LiveGateInputs) -> LiveGateDecision:
    """Evaluate every LIVE requirement; any single failure blocks arming."""
    failures: list[str] = []
    for field_info in fields(LiveGateInputs):
        name = field_info.name
        if name in _REQUIREMENT_LABELS:
            if getattr(inputs, name) is not True:
                failures.append(_REQUIREMENT_LABELS[name])
    if inputs.typed_confirmation_phrase != LIVE_CONFIRMATION_PHRASE:
        failures.append(f"the typed confirmation phrase does not match '{LIVE_CONFIRMATION_PHRASE}'")
    if inputs.second_confirmation_acknowledged is not True:
        failures.append("the second confirmation dialog (account/contract/size/stop/target/risk) was not acknowledged")
    return LiveGateDecision(allowed=not failures, failures=tuple(failures))


def read_live_enabled(production_config_path: Path) -> bool:
    """Read live_enabled/live_mode from the user-controlled production config.

    Missing file or missing key means FALSE. Generated code never writes this
    file; only a human editing it can flip it.
    """
    if not production_config_path.is_file():
        return False
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("live_enabled") is True or payload.get("live_mode") is True


@dataclass(frozen=True, slots=True)
class LiveGateApproval:
    """Proof object that every LIVE prerequisite passed at issuance time.

    Only :func:`issue_live_gate_approval` creates one, and only from a fully
    passing evaluation. It embeds the decision and the production-config path so
    the live gateway can re-verify ``live_enabled`` at construction (fail
    closed) - holding a stale approval is not enough.
    """

    decision: LiveGateDecision
    production_config_path: Path
    issued_for_account: str
    issued_utc: str


def issue_live_gate_approval(
    inputs: LiveGateInputs,
    *,
    production_config_path: Path,
    account_spec: str,
) -> LiveGateApproval:
    """Issue an approval ONLY when every requirement passes; otherwise raise."""
    from datetime import UTC, datetime

    if not read_live_enabled(production_config_path):
        raise PermissionError("live_enabled is false in the production configuration")
    decision = evaluate_live_gate(inputs)
    if not decision.allowed:
        raise PermissionError("LIVE gate not passed: " + "; ".join(decision.failures))
    return LiveGateApproval(
        decision=decision,
        production_config_path=production_config_path,
        issued_for_account=account_spec,
        issued_utc=datetime.now(UTC).isoformat(),
    )


@dataclass(slots=True)
class ArmingState:
    """In-memory arming state. Never serialized: every startup is disarmed."""

    live_armed: bool = False
    demo_armed: bool = False

    def arm_live(self, decision: LiveGateDecision) -> None:
        """Arm LIVE only with a fully passed gate decision; else raise."""
        if not decision.allowed:
            raise PermissionError("LIVE gate not passed: " + "; ".join(decision.failures))
        self.live_armed = True

    def disarm_all(self) -> None:
        """Return to the safe state."""
        self.live_armed = False
        self.demo_armed = False


@dataclass(frozen=True, slots=True)
class SecondConfirmationSummary:
    """Exactly what the second LIVE confirmation dialog must display."""

    account_spec: str
    contract: str
    max_contracts: int
    stop: Decimal
    target: Decimal
    worst_case_risk: Decimal

    def display_lines(self) -> tuple[str, ...]:
        """Return the human-readable confirmation lines."""
        return (
            f"Account: {self.account_spec}",
            f"Contract: {self.contract}",
            f"Maximum contracts: {self.max_contracts}",
            f"Stop: {self.stop}",
            f"Target: {self.target}",
            f"Worst-case risk: ${self.worst_case_risk}",
        )
