"""MNQ vs NQ contract multiplier and its effect on P&L and risk sizing."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.instruments import MNQ, NQ, resolve_instrument
from app.paper.models import points_to_dollars


def test_point_values() -> None:
    assert MNQ.point_value == Decimal("2")    # $2 / index point
    assert NQ.point_value == Decimal("20")    # $20 / index point
    assert NQ.tick_value == Decimal("5.00")
    assert MNQ.tick_size == NQ.tick_size       # identical price geometry


def test_resolve_is_forgiving_and_defaults_to_mnq() -> None:
    assert resolve_instrument("nq") is NQ
    assert resolve_instrument("  MNQ ") is MNQ
    assert resolve_instrument("nonsense") is MNQ
    assert resolve_instrument(None) is MNQ


def test_pnl_scales_10x_from_mnq_to_nq() -> None:
    points = Decimal("8.50")  # a real winning move: 29191.50 -> 29200
    assert points_to_dollars(points, 1, MNQ.tick_value) == Decimal("17.00")
    assert points_to_dollars(points, 1, NQ.tick_value) == Decimal("170.00")
    # Default (no tick_value) stays MNQ so existing behaviour is unchanged.
    assert points_to_dollars(points, 1) == Decimal("17.00")


def test_nq_risk_sizing_takes_fewer_contracts_than_mnq() -> None:
    """The dollar-based sizer allocates ~10x fewer NQ contracts for equal risk."""
    from app.paper.execution import size_intent
    from app.paper.models import Direction, PaperOrderIntent, SetupProvenance

    intent = PaperOrderIntent(
        direction=Direction.LONG, entry_reference=Decimal("29200.00"),
        stop=Decimal("29190.00"), target=Decimal("29230.00"),  # 10-pt (40-tick) stop
        provenance=SetupProvenance(session_id="s", setup_id="s:1", strategy_version="v",
                                   contract="MNQ", decision_event_index=1, decision_ts_ns=1,
                                   conditions_passed=()),
    )
    common = dict(balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                  max_contracts=20, commission_per_contract=Decimal("1.24"))
    mnq = size_intent(intent, tick_value=MNQ.tick_value, **common)
    nq = size_intent(intent, tick_value=NQ.tick_value, **common)
    # Honest, important behaviour: MNQ is sized normally, but a single NQ
    # contract at a 10-pt stop risks ~$200 and the $1,000-drawdown budget
    # refuses it entirely - so NQ on this micro profile takes ZERO trades.
    assert mnq.approved and mnq.contracts >= 1
    assert nq.contracts < mnq.contracts
    assert not nq.approved and nq.reason_code == "risk_zero_contracts"


def test_config_reader_selects_instrument(tmp_path: Path) -> None:
    from app.paper.options import read_instrument

    assert read_instrument(tmp_path / "missing.yaml") == "MNQ"
    nq = tmp_path / "nq.yaml"
    nq.write_text("paper_instrument: NQ\n", encoding="utf-8")
    assert read_instrument(nq) == "NQ"
    junk = tmp_path / "junk.yaml"
    junk.write_text("paper_instrument: banana\n", encoding="utf-8")
    assert read_instrument(junk) == "MNQ", "unknown instrument fails safe to MNQ"


def test_engine_reports_active_instrument() -> None:
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.research.episode_builder import EpisodeConfig

    mnq_engine = DelayedPaperEngine()
    assert mnq_engine.status().instrument == "MNQ"
    nq_engine = DelayedPaperEngine(config=EpisodeConfig(instrument="NQ"))
    assert nq_engine.status().instrument == "NQ"


def test_both_launchers_wire_the_instrument() -> None:
    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        source = Path(launcher).read_text(encoding="utf-8")
        assert "read_instrument" in source, launcher
