"""Tests for the automatic SHADOW runtime."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.market.contract_resolver import ContractResolver
from app.market.regime_classifier import (
    BehaviorRegime,
    LiquidityRegime,
    RegimeClassification,
    RegimeClassifier,
    VolatilityRegime,
)
from app.market.session_context import SessionContextResolver
from app.market.state import MarketState
from app.runtime.controller import AutomaticRuntimeController
from app.runtime.lifecycle import RuntimeState
from app.strategy.dynamic_thresholds import DynamicThresholdEngine, RollingBaseline
from app.strategy.profile_registry import ProfileRegistry
from app.strategy.session_router import SessionRouter
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult
from tools.start_assistant import AssistantConfig, _finalize_assistant_session, find_repo_root, parse_args


def test_session_context_detects_new_york_open_across_dst_offsets() -> None:
    """Session detection is timezone-aware and works in summer and winter."""
    resolver = SessionContextResolver.from_yaml("config/session_profiles.yaml")

    summer = resolver.resolve(datetime(2026, 7, 10, 13, 45, tzinfo=UTC))
    winter = resolver.resolve(datetime(2026, 1, 15, 14, 45, tzinfo=UTC))

    assert summer.name == "new_york_open"
    assert summer.minutes_since_open == 15
    assert summer.new_york_time.hour == 9
    assert summer.london_time.hour == 14
    assert winter.name == "new_york_open"
    assert winter.minutes_since_open == 15
    assert winter.new_york_time.hour == 9


def test_contract_resolver_blocks_non_mnq_wrong_contract_and_dual_contracts() -> None:
    """The resolver requires exact current MNQ aliases and blocks dual-contract ambiguity."""
    resolver = ContractResolver()
    trading_date = date(2026, 7, 10)

    assert resolver.current_contract_for_date(trading_date) == "MNQU6"
    non_mnq = resolver.resolve_alias("ESU6", trading_date)
    current = resolver.observe_alias("MNQU6", trading_date)
    dual = resolver.observe_alias("MNQZ6", trading_date)

    assert non_mnq.decisions_allowed is False
    assert non_mnq.requires_attention is True
    assert current.decisions_allowed is True
    assert dual.decisions_allowed is False
    assert "Multiple MNQ contracts" in dual.reason


def test_regime_classifier_blocks_insufficient_and_stale_data_then_classifies() -> None:
    """Regime classification stays unknown until enough fresh complete data exists."""
    classifier = RegimeClassifier(min_samples=5)
    snapshots = _market_snapshots(count=5)

    insufficient = classifier.classify(snapshots[:2])
    stale = classifier.classify(snapshots, current_timestamp_ns=snapshots[-1].timestamp_ns + 10_000_000_000)
    classified = classifier.classify(snapshots, current_timestamp_ns=snapshots[-1].timestamp_ns)

    assert insufficient.decisions_allowed is False
    assert insufficient.volatility == VolatilityRegime.UNKNOWN
    assert stale.decisions_allowed is False
    assert "stale" in stale.reason
    assert classified.decisions_allowed is True
    assert classified.liquidity == LiquidityRegime.NORMAL
    assert classified.behavior == BehaviorRegime.TRENDING


def test_profile_router_uses_exact_profile_then_global_observe_only_fallback() -> None:
    """Profile routing is deterministic and exposes the fallback level."""
    registry = ProfileRegistry.from_yaml("config/session_profiles.yaml")
    router = SessionRouter(registry)
    session = SessionContextResolver.from_yaml("config/session_profiles.yaml").resolve(
        datetime(2026, 7, 10, 13, 45, tzinfo=UTC),
    )
    exact_regime = RegimeClassification(
        volatility=VolatilityRegime.NORMAL,
        liquidity=LiquidityRegime.NORMAL,
        behavior=BehaviorRegime.TRENDING,
        sample_count=20,
        decisions_allowed=True,
        reason="test",
    )
    unknown_regime = RegimeClassification(
        volatility=VolatilityRegime.UNKNOWN,
        liquidity=LiquidityRegime.UNKNOWN,
        behavior=BehaviorRegime.UNKNOWN,
        sample_count=1,
        decisions_allowed=False,
        reason="test",
    )

    exact = router.route(session, exact_regime)
    blocked = router.route(session, unknown_regime)

    assert exact.selection.profile.profile_id == "ny_open_normal_trend"
    assert exact.selection.fallback_level == "exact"
    assert exact.decisions_allowed is True
    assert blocked.decisions_allowed is False
    assert "regime blocks decisions" in blocked.reason


def test_dynamic_thresholds_use_fallback_then_past_only_baseline() -> None:
    """Dynamic thresholds report fallback/provisional and historical threshold reasons."""
    engine = DynamicThresholdEngine(conservative_fallbacks={"aggressive_sell_volume": Decimal("400")})
    empty = RollingBaseline("aggressive_sell_volume")

    fallback = engine.evaluate(
        "aggressive_sell_volume",
        Decimal("300"),
        {"kind": "percentile", "percentile": 50},
        empty,
    )
    baseline = RollingBaseline.from_values(
        "aggressive_sell_volume",
        (Decimal("100"), Decimal("200"), Decimal("300")),
    )
    historical = engine.evaluate(
        "aggressive_sell_volume",
        Decimal("250"),
        {"kind": "percentile", "percentile": 50},
        baseline,
    )
    baseline.append(Decimal("1000"))

    assert fallback.threshold_used == Decimal("400")
    assert fallback.provisional is True
    assert fallback.passed is False
    assert historical.threshold_used == Decimal("200")
    assert historical.normalized_value == Decimal("1.25")
    assert historical.passed is True


def test_runtime_controller_blocks_duplicates_writes_reports_and_stays_shadow_only(tmp_path: Path) -> None:
    """The controller records decisions/reports without importing order execution."""
    controller = AutomaticRuntimeController.from_config(
        "config/session_profiles.yaml",
        report_root=tmp_path / "reports",
    )

    assert controller.start().state == RuntimeState.WAITING_FOR_BOOKMAP.value
    controller.handle_control_event({"type": "connected", "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30)})
    controller.handle_control_event({"type": "realtime_started", "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30)})
    controller.handle_control_event({"type": "replay_started", "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30)})
    for event in _book_events():
        controller.handle_market_event(event, current_timestamp_ns=_event_timestamp(event))

    result = SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(SetupConditionResult("test_condition", True, "test condition passed"),),
    )
    first = controller.record_setup_decision("setup-1", result, timestamp=datetime(2026, 7, 10, 13, 31, tzinfo=UTC))
    duplicate = controller.record_setup_decision(
        "setup-1",
        result,
        timestamp=datetime(2026, 7, 10, 13, 32, tzinfo=UTC),
    )
    report_dir = controller.finalize_session_report(
        session_id="session_test",
        session_date=date(2026, 7, 10),
        recorder_manifest={"valid_for_analysis": True},
    )

    assert first["mode"] == "SHADOW"
    assert duplicate["decision"] == "rejected"
    assert duplicate["reason"] == ["duplicate setup episode suppressed"]
    assert (report_dir / "summary.json").exists()
    assert (report_dir / "decisions.jsonl").exists()
    assert (report_dir / "detected_setups.csv").exists()
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["execution_data_included"] is False
    assert summary["broker_execution_data"] is None

    root = Path(__file__).resolve().parents[1]
    combined = "\n".join(
        (root / path).read_text(encoding="utf-8").lower()
        for path in ("app/runtime/controller.py", "tools/start_assistant.py")
    )
    assert "app.execution" not in combined
    assert "tradovate" not in combined


def test_runtime_delayed_bookmap_mode_records_only_and_blocks_shadow_decisions(tmp_path: Path) -> None:
    """Bookmap free delayed data is visible/recorded but never decision-ready."""
    controller = AutomaticRuntimeController.from_config(
        "config/session_profiles.yaml",
        report_root=tmp_path / "reports",
    )

    controller.start()
    controller.handle_control_event({"type": "connected", "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30)})
    controller.handle_control_event(
        {
            "type": "delayed_mode",
            "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30),
            "source_mode": "delayed",
            "delay_minutes": 15,
            "reason": "Bookmap free delayed data feed",
        },
    )
    controller.handle_control_event({"type": "realtime_started", "timestamp_ns": _timestamp_ns(2026, 7, 10, 13, 30)})
    for event in _book_events():
        controller.handle_market_event(event, current_timestamp_ns=_event_timestamp(event))

    result = SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(SetupConditionResult("test_condition", True, "test condition passed"),),
    )
    decision = controller.record_setup_decision(
        "delayed-setup",
        result,
        timestamp=datetime(2026, 7, 10, 13, 31, tzinfo=UTC),
    )
    snapshot = controller.snapshot()

    assert snapshot.source_mode == "delayed"
    assert snapshot.data_delay_minutes == 15
    assert snapshot.decisions_allowed is False
    assert snapshot.state == RuntimeState.RECORDING_ONLY.value
    assert decision["decision"] == "rejected"
    assert decision["reason"] == ["runtime blocked decisions"]


def test_zero_bridge_drops_do_not_create_false_data_gap() -> None:
    """Heartbeat counters at zero stay healthy and do not pollute daily reports."""
    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    controller.handle_control_event({"type": "connected", "timestamp_ns": 1, "dropped_message_count": 0})
    controller.handle_control_event({"type": "heartbeat", "timestamp_ns": 2, "dropped_message_count": 0})
    assert controller.health.dropped_message_count == 0
    assert not any(event.status == "data_gap" for event in controller.health.events)


def test_start_assistant_parses_config_and_batch_uses_local_venv_python() -> None:
    """The one-click entry point keeps the expected endpoint and local Python launcher."""
    config = parse_args(["--no-gui", "--port", "9009", "--output-root", "data/raw-test"])
    repo_root = find_repo_root(Path.cwd())
    batch_text = (repo_root / "start_mnq_assistant.bat").read_text(encoding="utf-8")

    assert config == AssistantConfig(
        port=9009,
        output_root=Path("data/raw-test"),
        gui=False,
    )
    launch_lines = [line for line in batch_text.splitlines() if line.startswith('"%PYTHON%"')]
    assert ".venv\\Scripts\\python.exe" in batch_text
    assert 'cd /d "%~dp0"' in batch_text
    assert len(launch_lines) == 1
    assert "-m tools.start_assistant" in launch_lines[0]
    assert "--delayed-data-minutes 15" in launch_lines[0]
    assert ".py" not in launch_lines[0]
    assert "admin" not in batch_text.lower()


def test_assistant_session_finalization_writes_daily_learning_report(tmp_path: Path) -> None:
    """The one-click assistant refreshes daily learning reports after a Bookmap session."""
    controller = AutomaticRuntimeController.from_config(
        "config/session_profiles.yaml",
        report_root=tmp_path / "reports",
    )
    config = AssistantConfig(
        output_root=tmp_path / "raw",
        report_root=tmp_path / "reports",
        gui=False,
    )
    recorder = MarketSessionRecorder(
        root_dir=config.output_root,
        session_start_utc=datetime(2026, 7, 10, 14, 30, tzinfo=UTC),
    )
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    recorder.record_control_event(
        {
            "type": "delayed_mode",
            "timestamp_ns": timestamp_ns,
            "source_mode": "delayed",
            "delay_minutes": 15,
            "reason": "Bookmap free delayed data feed",
        },
    )
    recorder.record(
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 1,
            "symbol": "MNQU6",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "120",
        },
    )
    recorder.record(
        {
            "type": "trade",
            "timestamp_ns": timestamp_ns + 2,
            "price": "100.00",
            "size": "10",
            "aggressor_side": "sell",
            "instrument": "MNQU6",
            "sequence_id": 1,
        },
    )
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": timestamp_ns + 3})

    _finalize_assistant_session(controller, config, recorder)

    report_dir = tmp_path / "reports" / "2026-07-10" / recorder.session_id
    learning_report = tmp_path / "reports" / "daily_learning" / "2026-07-10" / "daily_learning.md"
    learning_json = tmp_path / "reports" / "daily_learning" / "2026-07-10" / "daily_learning.json"
    assert (report_dir / "summary.json").exists()
    assert learning_report.exists()
    assert learning_json.exists()
    assert json.loads(learning_json.read_text(encoding="utf-8"))["auto_retraining_enabled"] is False


def _market_snapshots(*, count: int) -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    start_ns = _timestamp_ns(2026, 7, 10, 13, 30)
    for index in range(count):
        bid = Decimal("100.00") + Decimal(index) * Decimal("0.25")
        ask = bid + Decimal("0.25")
        state = state.update(
            {
                "type": "depth_update",
                "timestamp": start_ns + index * 1_000_000_000,
                "symbol": "MNQU6",
                "side": "bid",
                "price": str(bid),
                "previous_size": "0",
                "new_size": "100",
            },
        )
        state = state.update(
            {
                "type": "depth_update",
                "timestamp": start_ns + index * 1_000_000_000 + 1,
                "symbol": "MNQU6",
                "side": "ask",
                "price": str(ask),
                "previous_size": "0",
                "new_size": "100",
            },
        )
        snapshots.append(state)
    return tuple(snapshots)


def _book_events() -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    start_ns = _timestamp_ns(2026, 7, 10, 13, 30)
    for index in range(6):
        bid = Decimal("100.00") + Decimal(index) * Decimal("0.25")
        ask = bid + Decimal("0.25")
        events.extend(
            [
                {
                    "type": "depth_update",
                    "timestamp": start_ns + index * 1_000_000_000,
                    "symbol": "MNQU6",
                    "side": "bid",
                    "price": str(bid),
                    "previous_size": "0",
                    "new_size": "100",
                },
                {
                    "type": "depth_update",
                    "timestamp": start_ns + index * 1_000_000_000 + 1,
                    "symbol": "MNQU6",
                    "side": "ask",
                    "price": str(ask),
                    "previous_size": "0",
                    "new_size": "100",
                },
            ],
        )
    return tuple(events)


def _event_timestamp(event: dict[str, object]) -> int:
    return int(event["timestamp"])


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(timestamp.timestamp()) * 1_000_000_000
