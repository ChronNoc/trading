"""Proof: the shadow model's output can change (or safely fail to change) a
real paper decision, gated by ``EpisodeConfig.ml_decision_policy_enabled``.

Uses the SAME deterministic accepted-long fixture as
``tests/test_paper_lifecycle.py`` (``_absorption_long_events``), so the
heuristic-accepted baseline is already proven elsewhere; these tests isolate
the ML veto-only policy layered on top of it via a fake/stub model_loader -
never a real artifact, never touching live execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.paper.streaming_engine import (
    DECISION_SOURCE_BLENDED,
    DECISION_SOURCE_FALLBACK,
    DECISION_SOURCE_HEURISTIC,
    DECISION_SOURCE_ML,
    ML_VETO_PROBABILITY_THRESHOLD,
    DelayedPaperEngine,
)
from app.research.episode_builder import EpisodeConfig

NEW_YORK = ZoneInfo("America/New_York")
SEC = 1_000_000_000
BASE_NS = int(datetime(2026, 7, 16, 10, 5, tzinfo=NEW_YORK).timestamp()) * SEC


def _price(offset: str) -> str:
    return f"{Decimal(offset) + Decimal('29400'):.2f}"


def _depth(ts_ns: int, side: str, price: str, previous: str, new: str) -> dict[str, object]:
    return {"type": "depth_update", "timestamp": ts_ns, "symbol": "MNQ", "side": side,
            "price": price, "previous_size": previous, "new_size": new}


def _trade(ts_ns: int, price: str, size: str, side: str, seq: int) -> dict[str, object]:
    return {"type": "trade", "timestamp_ns": ts_ns, "instrument": "MNQ", "price": price,
            "size": size, "aggressor_side": side, "sequence_id": seq}


def _absorption_long_events() -> list[dict[str, object]]:
    """Same deterministic bid-absorption-then-buy-continuation tape as
    ``tests/test_paper_lifecycle.py`` - proven elsewhere to be heuristically
    ACCEPTED (see ``test_a_qualifying_setup_automatically_opens_a_simulated_position``).
    """
    return [
        _depth(BASE_NS, "bid", _price("100.00"), "0", "120"),
        _depth(BASE_NS, "bid", _price("99.75"), "0", "70"),
        _depth(BASE_NS, "ask", _price("100.25"), "0", "100"),
        _depth(BASE_NS, "ask", _price("102.00"), "0", "120"),
        _trade(BASE_NS + SEC, _price("100.00"), "420", "sell", 1),      # absorbed
        _depth(BASE_NS + SEC, "bid", _price("100.00"), "120", "20"),
        _depth(BASE_NS + 2 * SEC, "bid", _price("100.00"), "20", "135"),  # reload
        _depth(BASE_NS + 3 * SEC, "ask", _price("100.25"), "100", "10"),
        _depth(BASE_NS + 4 * SEC, "ask", _price("100.25"), "10", "0"),
        _depth(BASE_NS + 4 * SEC, "ask", _price("100.75"), "0", "40"),
        _depth(BASE_NS + 4 * SEC, "bid", _price("100.50"), "0", "70"),
        _depth(BASE_NS + 4 * SEC, "bid", _price("100.00"), "135", "130"),
        _trade(BASE_NS + 5 * SEC, _price("100.75"), "80", "buy", 2),
        _depth(BASE_NS + 5 * SEC, "bid", _price("100.00"), "130", "140"),
        _trade(BASE_NS + 6 * SEC, _price("100.75"), "70", "buy", 3),
    ]


@dataclass(frozen=True, slots=True)
class _StubSnapshot:
    state: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class _StubEvidence:
    prediction_id: str
    artifact_id: str
    artifact_sha256: str
    session_id: str
    direction: str
    timestamp_ns: int
    success_probability: float


class _StubModelLoader:
    """A deterministic fake matching the loader's atomic evidence interface."""

    def __init__(
        self,
        *,
        state: str = "SCORING",
        artifact_id: str = "test-artifact-1",
        snapshot_artifact_id: str | None = None,
        artifact_sha256: str = "a" * 64,
        prediction_id: str = "test-prediction-1",
        probability: float | None = None,
        evidence_timestamp_ns: int | None = None,
        evidence_session_id: str = "session_ml_policy",
        evidence_direction: str | None = None,
    ) -> None:
        self._state = state
        self._artifact_id = artifact_id
        self._snapshot_artifact_id = snapshot_artifact_id or artifact_id
        self._artifact_sha256 = artifact_sha256
        self._prediction_id = prediction_id
        self._probability = probability
        self._evidence_timestamp_ns = evidence_timestamp_ns
        self._evidence_session_id = evidence_session_id
        self._evidence_direction = evidence_direction

    def snapshot(self) -> _StubSnapshot:
        return _StubSnapshot(state=self._state, artifact_id=self._snapshot_artifact_id)

    def last_probability(
        self,
        direction: str,
        *,
        session_id: str,
        as_of_timestamp_ns: int,
        max_age_ns: int,
    ) -> _StubEvidence | None:
        if self._probability is None or session_id != self._evidence_session_id:
            return None
        evidence_timestamp_ns = (
            as_of_timestamp_ns
            if self._evidence_timestamp_ns is None
            else self._evidence_timestamp_ns
        )
        age_ns = as_of_timestamp_ns - evidence_timestamp_ns
        if age_ns < 0 or age_ns > max_age_ns:
            return None
        return _StubEvidence(
            prediction_id=self._prediction_id,
            artifact_id=self._artifact_id,
            artifact_sha256=self._artifact_sha256,
            session_id=self._evidence_session_id,
            direction=self._evidence_direction or direction,
            timestamp_ns=evidence_timestamp_ns,
            success_probability=self._probability,
        )


class _BrokenModelLoader:
    """Simulates a model loader whose snapshot()/last_probability() raise."""

    def snapshot(self) -> _StubSnapshot:
        raise RuntimeError("boom")

    def last_probability(
        self,
        direction: str,
        **correlation: object,
    ) -> _StubEvidence | None:
        del direction, correlation
        raise RuntimeError("boom")


class _CallTrackingLoader:
    """Records every method invocation, then raises - proves zero-touch, not
    just safe-if-touched. ``_BrokenModelLoader`` alone cannot distinguish
    "never called" from "called but the broad except swallowed it", since
    ``_apply_ml_policy`` fails safe to the heuristic outcome either way. This
    loader also raises on ANY attribute access at all (not just the two
    methods the enabled path uses), so a future refactor that reads a new
    attribute off the loader would be caught here too.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise RuntimeError(f"policy-off path must never touch model_loader.{name}")


def _engine(*, ml_decision_policy_enabled: bool, model_loader: object | None) -> DelayedPaperEngine:
    return DelayedPaperEngine(
        config=EpisodeConfig(
            warmup_events=1, decision_stride=1, warmup_span_seconds=0,
            evaluation_interval_ms=0, depth_sample_interval_ms=0,
            ml_decision_policy_enabled=ml_decision_policy_enabled,
        ),
        is_synthetic_fixture=True,
        model_loader=model_loader,
    )


def _run(engine: DelayedPaperEngine) -> DelayedPaperEngine:
    engine.bind_session("session_ml_policy", "MNQU6")
    for event in _absorption_long_events():
        engine.on_market_event(event)
    return engine


# --- proof: model output CAN change a shadow decision (step 5) -------------------


def test_low_probability_vetoes_a_heuristic_accepted_setup_and_blocks_the_order() -> None:
    """A confident negative shadow score overrides ACCEPT -> REJECT, veto-only."""
    loader = _StubModelLoader(state="SCORING", probability=ML_VETO_PROBABILITY_THRESHOLD - 0.10)
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()

    vetoed_records = [
        evaluation
        for evaluation in engine.recent_evaluations(limit=50)
        if evaluation.decision_source == DECISION_SOURCE_BLENDED
    ]
    assert vetoed_records, "the fixture's heuristic ACCEPT must be visibly changed by the ML veto"
    record = vetoed_records[-1]

    assert record.decision_source == DECISION_SOURCE_BLENDED
    assert record.accepted is False
    assert record.confidence == ML_VETO_PROBABILITY_THRESHOLD - 0.10
    assert record.raw_model_output == {
        "success_probability": ML_VETO_PROBABILITY_THRESHOLD - 0.10,
    }
    assert record.model_version == "test-artifact-1"
    assert record.model_prediction_id == "test-prediction-1"
    assert record.model_artifact_sha256 == "a" * 64
    assert status.orders_submitted == 0, "a vetoed setup must never become a paper order"
    assert status.accepted_setups == 0


def test_high_probability_keeps_a_heuristic_accepted_setup_and_submits_the_order() -> None:
    """A confident positive shadow score does not veto; ACCEPT is preserved."""
    loader = _StubModelLoader(state="SCORING", probability=0.90)
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()

    accepted_records = [r for r in engine.recent_evaluations(limit=50) if r.accepted]
    assert accepted_records, "the fixture must still genuinely pass the heuristic"
    record = accepted_records[-1]

    assert record.decision_source == DECISION_SOURCE_ML
    assert record.confidence == 0.90
    assert record.raw_model_output == {"success_probability": 0.90}
    assert record.model_version == "test-artifact-1"
    assert record.model_prediction_id == "test-prediction-1"
    assert record.model_artifact_sha256 == "a" * 64
    assert status.orders_submitted == 1, "a non-vetoed accepted setup must still submit an order"
    assert status.accepted_setups > 0


def test_snapshot_identity_cannot_override_correlated_evidence_identity() -> None:
    loader = _StubModelLoader(
        state="SCORING",
        snapshot_artifact_id="raced-snapshot-artifact",
        artifact_id="evidence-artifact",
        artifact_sha256="b" * 64,
        prediction_id="evidence-prediction",
        probability=0.90,
    )
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.model_version == "evidence-artifact"
    assert record.model_prediction_id == "evidence-prediction"
    assert record.model_artifact_sha256 == "b" * 64


def test_veto_only_never_accepts_a_heuristic_rejected_setup() -> None:
    """The model can tighten (veto) but never loosen (accept a rejection)."""
    loader = _StubModelLoader(state="SCORING", probability=0.99)
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    rejected = [r for r in engine.recent_evaluations(limit=500) if not r.accepted]
    # Every rejected evaluation must still be rejected - a high probability
    # must never flip a heuristic REJECT to ACCEPT.
    for record in rejected:
        assert record.accepted is False


# --- fail-safe fallback: broken/absent/non-scoring loader never blocks paper ----


def test_policy_enabled_with_no_model_loader_falls_back_to_heuristic_outcome() -> None:
    """No model_loader at all: behaviour must match the heuristic-only outcome."""
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=None))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert record.fallback_reason
    assert status.orders_submitted == 1, "fallback must never disable the heuristic path"


def test_policy_enabled_with_non_scoring_loader_falls_back_to_heuristic_outcome() -> None:
    """No approved/SCORING model: behaviour must match the heuristic-only outcome."""
    loader = _StubModelLoader(state="NOT_APPROVED", probability=0.01)
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert "state=NOT_APPROVED" in record.fallback_reason
    assert status.orders_submitted == 1, "a non-SCORING model must never veto"


def test_policy_enabled_with_broken_loader_never_crashes_or_blocks_paper() -> None:
    """A loader whose snapshot()/last_probability() raise must not stop evaluation."""
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=_BrokenModelLoader()))
    status = engine.status()
    assert status.evaluations > 0
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)
    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert status.orders_submitted == 1, "a broken loader must fail safe to heuristic"


def test_no_probability_for_direction_falls_back_to_heuristic_outcome() -> None:
    """SCORING but nothing scored yet for this direction: fall back, don't guess."""
    loader = _StubModelLoader(state="SCORING", probability=None)
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert "no matching prediction" in record.fallback_reason
    assert status.orders_submitted == 1


# --- default-off regression proof (step 6) ---------------------------------------


def test_stale_prediction_cannot_veto_a_heuristic_accepted_setup() -> None:
    loader = _StubModelLoader(
        state="SCORING",
        probability=0.01,
        evidence_timestamp_ns=BASE_NS - 120 * SEC,
    )
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert "correlation window" in record.fallback_reason
    assert record.confidence is None
    assert record.model_version == ""
    assert record.model_prediction_id == ""
    assert record.model_artifact_sha256 == ""
    assert status.orders_submitted == 1


def test_future_dated_prediction_cannot_veto_a_heuristic_accepted_setup() -> None:
    loader = _StubModelLoader(
        state="SCORING",
        probability=0.01,
        evidence_timestamp_ns=BASE_NS + 120 * SEC,
    )
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert "correlation window" in record.fallback_reason
    assert record.confidence is None
    assert record.model_version == ""
    assert record.model_prediction_id == ""
    assert record.model_artifact_sha256 == ""
    assert status.orders_submitted == 1


def test_prior_session_prediction_cannot_veto_a_heuristic_accepted_setup() -> None:
    loader = _StubModelLoader(
        state="SCORING",
        probability=0.01,
        evidence_session_id="previous_session",
    )
    engine = _run(_engine(ml_decision_policy_enabled=True, model_loader=loader))
    status = engine.status()
    record = next(r for r in engine.recent_evaluations(limit=50) if r.accepted)

    assert record.decision_source == DECISION_SOURCE_FALLBACK
    assert "correlation window" in record.fallback_reason
    assert record.confidence is None
    assert record.model_version == ""
    assert record.model_prediction_id == ""
    assert record.model_artifact_sha256 == ""
    assert status.orders_submitted == 1


def test_default_config_disables_the_ml_policy() -> None:
    assert EpisodeConfig().ml_decision_policy_enabled is False


def test_ml_policy_disabled_by_default_is_byte_identical_to_pre_change_behaviour() -> None:
    """The default (policy off) must reproduce the EXACT pre-existing outcome:
    one accepted long setup, one submitted order, decision_source HEURISTIC -
    even with a model_loader attached and a low (would-be-veto) probability.
    """
    loader = _StubModelLoader(state="SCORING", probability=0.01)  # would veto if consulted
    engine = _run(_engine(ml_decision_policy_enabled=False, model_loader=loader))
    status = engine.status()

    assert status.accepted_setups > 0
    assert status.orders_submitted == 1
    assert status.open_position.startswith("long")
    for record in engine.recent_evaluations(limit=500):
        assert record.decision_source == DECISION_SOURCE_HEURISTIC
        assert record.confidence is None
        assert record.fallback_reason == "policy disabled"
        assert record.raw_model_output is None
        assert record.model_version == ""
        assert record.model_prediction_id == ""
        assert record.model_artifact_sha256 == ""


def test_ml_policy_disabled_ignores_model_loader_entirely_even_when_broken() -> None:
    """Policy off must not even consult a broken loader - true byte-identical path."""
    engine = _run(_engine(ml_decision_policy_enabled=False, model_loader=_BrokenModelLoader()))
    status = engine.status()
    assert status.orders_submitted == 1
    assert status.accepted_setups > 0


def test_ml_policy_disabled_never_calls_any_model_loader_method_or_attribute() -> None:
    """Stronger guarantee than the broken-loader test above: prove ZERO attribute
    access on model_loader when the policy is off, not merely "safe if touched".
    ``_CallTrackingLoader`` raises on every attribute access and records the
    name first, so if ``_apply_ml_policy`` ever reads ``.snapshot``,
    ``.last_probability``, or anything else off the loader while disabled,
    this fails loudly with the exact attribute name that leaked through -
    matching the byte-identical-path guarantee in
    docs/model_governance_and_shadow_policy.md section 8.
    """
    loader = _CallTrackingLoader()
    engine = _run(_engine(ml_decision_policy_enabled=False, model_loader=loader))
    status = engine.status()

    assert loader.calls == [], f"policy-off path touched model_loader attributes: {loader.calls}"
    assert status.orders_submitted == 1
    assert status.accepted_setups > 0
    for record in engine.recent_evaluations(limit=500):
        assert record.decision_source == DECISION_SOURCE_HEURISTIC
        assert record.fallback_reason == "policy disabled"
