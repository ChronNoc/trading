"""Automatic one-process SHADOW runtime controller."""

from __future__ import annotations

import csv
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bookmap_addon.events import is_control_event

from app.market.contract_resolver import ContractResolution, ContractResolver
from app.market.regime_classifier import RegimeClassification, RegimeClassifier
from app.market.session_context import SessionContext, SessionContextResolver
from app.market.state import MarketState
from app.runtime.health import HealthMonitor
from app.runtime.lifecycle import RuntimeMode, RuntimeState, RuntimeStateMachine
from app.strategy.profile_registry import ProfileRegistry
from app.strategy.session_router import SessionRouter
from app.strategy.setups import SetupEvaluationResult

DEFAULT_WINDOW_SIZE = 240
DEFAULT_WARMUP_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """GUI-facing snapshot of the automatic runtime."""

    state: str
    mode: str
    bookmap_status: str
    recording: bool
    exact_contract: str
    contract_reason: str
    source_mode: str
    data_delay_minutes: int | None
    session_name: str
    session_date: str
    minutes_since_open: int | None
    minutes_until_close: int | None
    regime: str
    profile_id: str
    profile_fallback: str
    profile_validation: str
    historical_sample_count: int
    decisions_allowed: bool
    warmup_complete: bool
    sample_count: int
    data_age_ms: int | None
    dropped_message_count: int
    current_session_dropped_message_count: int
    threshold_summary: str
    shadow_decisions: int
    report_root: str


@dataclass(frozen=True, slots=True)
class ShadowSubmission:
    """A recorded shadow-only order intention."""

    setup_id: str
    requested_at_utc: str
    description: str


@dataclass(slots=True)
class ShadowExecutionStub:
    """No-op execution stub that records would-have-submitted messages."""

    submissions: list[ShadowSubmission] = field(default_factory=list)

    def would_submit(
        self,
        setup_id: str,
        description: str,
        *,
        now: datetime | None = None,
    ) -> ShadowSubmission:
        """Record a shadow order intention without reaching any broker module."""
        submission = ShadowSubmission(
            setup_id=setup_id,
            requested_at_utc=(now or datetime.now(UTC)).astimezone(UTC).isoformat(),
            description=f"would have submitted {description}",
        )
        self.submissions.append(submission)
        return submission


@dataclass(slots=True)
class AutomaticRuntimeController:
    """Coordinate receiver, market state, regime/profile routing, and reports."""

    session_resolver: SessionContextResolver
    regime_classifier: RegimeClassifier
    profile_registry: ProfileRegistry
    report_root: Path = Path("data/reports")
    lifecycle: RuntimeStateMachine = field(default_factory=RuntimeStateMachine)
    health: HealthMonitor = field(default_factory=HealthMonitor)
    contract_resolver: ContractResolver = field(default_factory=ContractResolver)
    shadow_execution: ShadowExecutionStub = field(default_factory=ShadowExecutionStub)
    warmup_samples: int = DEFAULT_WARMUP_SAMPLES
    market_state: MarketState = field(default_factory=MarketState)
    source_mode: str = "unknown"
    data_delay_minutes: int | None = None
    current_session: SessionContext | None = None
    current_regime: RegimeClassification | None = None
    current_profile_id: str = "unavailable"
    current_profile_fallback: str = "unavailable"
    current_profile_validation: str = "unavailable"
    current_historical_sample_count: int = 0
    current_contract: ContractResolution | None = None
    decisions_allowed: bool = False
    threshold_summary: str = "no thresholds evaluated yet"
    decisions: list[dict[str, object]] = field(default_factory=list)
    detected_setups: list[dict[str, object]] = field(default_factory=list)
    profile_transitions: list[dict[str, object]] = field(default_factory=list)
    _window: deque[MarketState] = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW_SIZE))
    _seen_setup_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_config(
        cls,
        config_path: str | Path = Path("config/session_profiles.yaml"),
        *,
        report_root: str | Path = Path("data/reports"),
    ) -> "AutomaticRuntimeController":
        """Create a controller from the shared session/profile YAML config."""
        return cls(
            session_resolver=SessionContextResolver.from_yaml(config_path),
            regime_classifier=RegimeClassifier(),
            profile_registry=ProfileRegistry.from_yaml(config_path),
            report_root=Path(report_root),
        )

    @property
    def mode(self) -> RuntimeMode:
        """Return the only supported automatic runtime mode."""
        return RuntimeMode.SHADOW

    def start(self, *, now: datetime | None = None) -> RuntimeSnapshot:
        """Start the runtime and wait for Bookmap."""
        self.lifecycle.start(now=now)
        self.health.record_event("runtime", "starting", "automatic runtime started in SHADOW mode", now=now)
        return self.snapshot()

    def stop(self, *, now: datetime | None = None) -> RuntimeSnapshot:
        """Stop the runtime."""
        self.lifecycle.stopping(now=now)
        self.health.record_event("runtime", "stopping", "automatic runtime stopping", now=now)
        self.lifecycle.stopped(now=now)
        return self.snapshot()

    def handle_control_event(
        self,
        event: Mapping[str, object],
        *,
        now: datetime | None = None,
    ) -> RuntimeSnapshot:
        """Handle a Bookmap bridge control event."""
        if not is_control_event(event):
            raise ValueError("expected a Bookmap control event")
        event_type = str(event["type"])
        if "dropped_message_count" in event:
            self.health.mark_dropped_messages(int(event["dropped_message_count"]), now=now)

        if event_type == "connected":
            self.health.mark_bookmap_connected(now=now)
            self.contract_resolver.reset_session()
            self.lifecycle.bookmap_connected(now=now)
        elif event_type == "disconnected":
            reason = str(event.get("reason", "Bookmap disconnected"))
            self.health.mark_bookmap_disconnected(reason, now=now)
            self.lifecycle.connection_lost(now=now)
        elif event_type in {"replay_started", "historical_mode"}:
            if self.source_mode != "delayed":
                self.source_mode = "replay"
                message = "Bookmap replay stream detected"
            else:
                message = "Bookmap delayed entitlement entered replay playback"
            self.health.record_event("bookmap", event_type, message, now=now)
        elif event_type == "prototype_mode":
            self.source_mode = "prototype"
            self.threshold_summary = "PROVISIONAL/SYNTHETIC dynamic thresholds from warm-up events"
            self.health.record_event("prototype", "prototype_mode", "Synthetic prototype stream detected", now=now)
        elif event_type == "delayed_mode":
            self.source_mode = "delayed"
            self.data_delay_minutes = _optional_int(event.get("delay_minutes"))
            delay_text = _delay_text(self.data_delay_minutes)
            self.threshold_summary = f"DELAYED {delay_text} Bookmap data - recording only; shadow decisions disabled"
            self.decisions_allowed = False
            self.lifecycle.recording_only("Bookmap delayed data recording only", now=now)
            self.health.record_event("bookmap", "delayed_mode", f"Bookmap delayed data mode: {delay_text}", now=now)
        elif event_type == "realtime_started":
            if self.source_mode not in {"prototype", "delayed"}:
                self.source_mode = "live"
                self.health.record_event("bookmap", "realtime_started", "Bookmap live stream detected", now=now)
            else:
                self.health.record_event("bookmap", "realtime_started", f"Bookmap stream detected as {self.source_mode}", now=now)
        elif event_type == "session_ended":
            self.health.record_event("bookmap", "session_ended", "Bookmap session ended", now=now)
            self.lifecycle.bookmap_connected(now=now)
        elif event_type == "data_gap":
            self.decisions_allowed = False
            self.health.record_event("bookmap", "data_gap", str(event.get("reason", "data gap")), now=now)
            self.lifecycle.data_stale(now=now)
        return self.snapshot()

    def handle_market_event(
        self,
        event: Mapping[str, object],
        *,
        current_timestamp_ns: int | None = None,
    ) -> RuntimeSnapshot:
        """Apply one market event, classify context, and update routing state."""
        if is_control_event(event):
            return self.handle_control_event(event)

        self.market_state = self.market_state.update(event)
        self.handle_market_state_snapshot(
            self.market_state,
            current_timestamp_ns=current_timestamp_ns,
            alias=_event_symbol(event),
        )
        return self.snapshot(current_timestamp_ns=current_timestamp_ns)

    def handle_prebuilt_state(self, event: Mapping[str, object], state: MarketState) -> None:
        """Consume a receiver-built state without recomputing it.

        The receiver already applied ``event`` to produce ``state``; rebuilding
        it here duplicated an O(book) copy per event on the capture path. Runs
        on the analysis thread, never on the receiver loop.
        """
        self.market_state = state
        self.handle_market_state_snapshot(state, alias=_event_symbol(event))

    def handle_market_state_snapshot(
        self,
        state: MarketState,
        *,
        current_timestamp_ns: int | None = None,
        alias: str | None = None,
    ) -> RuntimeSnapshot:
        """Process a market-state snapshot produced by the receiver."""
        self.market_state = state
        self._window.append(state)
        self.health.mark_market_event(state.timestamp_ns)
        event_instant = _datetime_from_ns(state.timestamp_ns)
        self.current_session = self.session_resolver.resolve(event_instant)
        if alias is not None and self.current_session.session_date is not None:
            if self.source_mode == "prototype":
                self.current_contract = ContractResolution(
                    alias=alias,
                    normalized_symbol=alias,
                    display_symbol=alias,
                    is_mnq=True,
                    is_current_contract=True,
                    decisions_allowed=True,
                    requires_attention=False,
                    reason="Synthetic prototype contract; no real trading enabled.",
                )
            else:
                self.current_contract = self.contract_resolver.observe_alias(alias, self.current_session.session_date)

        timestamp_for_stale_check = current_timestamp_ns if current_timestamp_ns is not None else state.timestamp_ns
        self.current_regime = self.regime_classifier.classify(
            tuple(self._window),
            current_timestamp_ns=timestamp_for_stale_check,
        )
        router = SessionRouter(self.profile_registry)
        routing = router.route(self.current_session, self.current_regime)
        profile = routing.selection.profile
        if profile.profile_id != self.current_profile_id:
            self._record_profile_transition(profile.profile_id, routing.selection.fallback_reason)
        self.current_profile_id = profile.profile_id
        self.current_profile_fallback = routing.selection.fallback_reason
        self.current_profile_validation = profile.validation_status
        self.current_historical_sample_count = profile.historical_sample_count

        contract_allowed = self.current_contract.decisions_allowed if self.current_contract is not None else True
        self.decisions_allowed = routing.decisions_allowed and contract_allowed
        self._update_lifecycle_from_context(current_timestamp_ns=timestamp_for_stale_check)
        return self.snapshot(current_timestamp_ns=timestamp_for_stale_check)

    def record_setup_decision(
        self,
        setup_id: str,
        result: SetupEvaluationResult,
        *,
        symbol: str = "MNQ",
        direction: str = "long",
        strategy_version: str = "rules-v0",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        """Record one shadow setup decision and prevent duplicate episodes."""
        timestamp_utc = (timestamp or datetime.now(UTC)).astimezone(UTC).isoformat()
        duplicate = setup_id in self._seen_setup_ids
        if duplicate:
            accepted = False
            reasons = ["duplicate setup episode suppressed"]
        else:
            self._seen_setup_ids.add(setup_id)
            accepted = result.accepted and self.decisions_allowed
            reasons = [condition.message for condition in result.conditions if not condition.passed]
            if result.accepted and not self.decisions_allowed:
                reasons.append("runtime blocked decisions")
            if accepted:
                submission = self.shadow_execution.would_submit(
                    setup_id,
                    f"{direction} {symbol} shadow bracket",
                    now=timestamp,
                )
                reasons.append(submission.description)

        decision = {
            "timestamp": timestamp_utc,
            "mode": self.mode.value,
            "setup_id": setup_id,
            "symbol": symbol,
            "direction": direction,
            "strategy_version": strategy_version,
            "model_version": None,
            "session": self.current_session.name if self.current_session else "unknown",
            "regime": self.current_regime.summary if self.current_regime else "unknown/unknown/unknown",
            "profile_id": self.current_profile_id,
            "checks": {condition.key: condition.passed for condition in result.conditions},
            "decision": "accepted" if accepted else "rejected",
            "reason": reasons,
            "broker_execution": None,
        }
        self.decisions.append(decision)
        self.detected_setups.append(
            {
                "timestamp": timestamp_utc,
                "setup_id": setup_id,
                "symbol": symbol,
                "direction": direction,
                "accepted": accepted,
                "duplicate": duplicate,
                "profile_id": self.current_profile_id,
                "regime": decision["regime"],
                "reason": "; ".join(reasons),
            },
        )
        if accepted:
            self.lifecycle.shadow_active()
        return decision

    def finalize_session_report(
        self,
        *,
        session_id: str,
        session_date: date,
        recorder_manifest: Mapping[str, object] | None = None,
    ) -> Path:
        """Write summary and append-only report artifacts for one session."""
        report_dir = self.report_root / session_date.isoformat() / session_id
        report_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "session_id": session_id,
            "session_date": session_date.isoformat(),
            "runtime_mode": self.mode.value,
            "final_state": self.lifecycle.state.value,
            "source_mode": self.source_mode,
            "data_delay_minutes": self.data_delay_minutes,
            "selected_profile": self.current_profile_id,
            "profile_fallback": self.current_profile_fallback,
            "profile_validation": self.current_profile_validation,
            "historical_sample_count": self.current_historical_sample_count,
            "decisions_allowed": self.decisions_allowed,
            "accepted_decisions": sum(1 for decision in self.decisions if decision["decision"] == "accepted"),
            "rejected_decisions": sum(1 for decision in self.decisions if decision["decision"] == "rejected"),
            "dropped_message_count": self.health.dropped_message_count,
            "lifetime_bridge_queue_drops": self.health.dropped_message_count,
            "current_session_bridge_queue_drops": self.health.current_session_dropped_message_count,
            "data_gaps": [
                event.message
                for event in self.health.events
                if event.status in {"data_gap", "disconnected", "failed"}
            ],
            "execution_data_included": False,
            "broker_execution_data": None,
            "recorder_manifest": dict(recorder_manifest or {}),
        }
        _write_json(report_dir / "summary.json", summary)
        _write_jsonl(report_dir / "decisions.jsonl", self.decisions)
        _write_jsonl(report_dir / "profile_transitions.jsonl", self.profile_transitions)
        self.health.write_events_jsonl(report_dir / "health_events.jsonl")
        _write_detected_setups_csv(report_dir / "detected_setups.csv", self.detected_setups)
        return report_dir

    def snapshot(self, *, current_timestamp_ns: int | None = None) -> RuntimeSnapshot:
        """Return the current runtime state for the GUI."""
        health = self.health.snapshot(current_timestamp_ns=current_timestamp_ns)
        session = self.current_session
        regime = self.current_regime
        contract = self.current_contract
        return RuntimeSnapshot(
            state=self.lifecycle.state.value,
            mode=self.mode.value,
            bookmap_status="connected" if health.bookmap_connected else "waiting",
            recording=health.recording,
            exact_contract=contract.display_symbol if contract is not None else "unknown",
            contract_reason=contract.reason if contract is not None else "awaiting exact MNQ contract alias",
            source_mode=self.source_mode,
            data_delay_minutes=self.data_delay_minutes,
            session_name=session.display_name if session is not None else "unknown",
            session_date=session.session_date.isoformat() if session is not None and session.session_date else "unknown",
            minutes_since_open=session.minutes_since_open if session is not None else None,
            minutes_until_close=session.minutes_until_close if session is not None else None,
            regime=regime.summary if regime is not None else "unknown/unknown/unknown",
            profile_id=self.current_profile_id,
            profile_fallback=self.current_profile_fallback,
            profile_validation=self.current_profile_validation,
            historical_sample_count=self.current_historical_sample_count,
            decisions_allowed=self.decisions_allowed,
            warmup_complete=len(self._window) >= self.warmup_samples,
            sample_count=len(self._window),
            data_age_ms=health.last_event_age_ms,
            dropped_message_count=health.dropped_message_count,
            current_session_dropped_message_count=health.current_session_dropped_message_count,
            threshold_summary=self.threshold_summary,
            shadow_decisions=len(self.decisions),
            report_root=str(self.report_root),
        )

    def _update_lifecycle_from_context(self, *, current_timestamp_ns: int) -> None:
        if self.health.is_data_stale(current_timestamp_ns):
            self.lifecycle.data_stale()
            self.decisions_allowed = False
            return
        if len(self._window) < self.warmup_samples:
            self.lifecycle.warmup()
            self.decisions_allowed = False
            return
        if (
            self.source_mode == "prototype"
            and (self.current_contract is None or self.current_contract.decisions_allowed)
        ):
            self.decisions_allowed = True
            self.lifecycle.shadow_ready()
            return
        if self.source_mode == "delayed":
            self.decisions_allowed = False
            self.lifecycle.recording_only("Bookmap delayed data recording only")
            return
        if self.current_regime is not None and not self.current_regime.decisions_allowed:
            self.lifecycle.data_stale()
            self.decisions_allowed = False
            return
        if self.current_contract is not None and not self.current_contract.decisions_allowed:
            self.lifecycle.profile_unavailable()
            self.decisions_allowed = False
            return
        if self.decisions_allowed:
            self.lifecycle.shadow_ready()
        else:
            self.lifecycle.profile_unavailable()

    def _record_profile_transition(self, profile_id: str, reason: str) -> None:
        self.profile_transitions.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "profile_id": profile_id,
                "fallback_reason": reason,
            },
        )


def _event_symbol(event: Mapping[str, object]) -> str | None:
    if "symbol" in event:
        return str(event["symbol"])
    if "instrument" in event:
        return str(event["instrument"])
    return None


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1_000)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _delay_text(delay_minutes: int | None) -> str:
    if delay_minutes is None:
        return "unknown-delay"
    return f"{delay_minutes}-minute"


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _write_detected_setups_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "timestamp",
        "setup_id",
        "symbol",
        "direction",
        "accepted",
        "duplicate",
        "profile_id",
        "regime",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
