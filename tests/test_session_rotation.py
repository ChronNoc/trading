"""Session rotation: a damaged session ends; a clean one begins; nothing hides.

The real failure this pins: one session was invalidated by bridge drops and the
application kept recording into it forever while the drop count climbed from
17,030 to 43,296. Every test uses temp directories and a fake clock.
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.session_rotation import RotatingRecorder, session_damage


class _FakeRecorder:
    """A minimal recorder with the damage counters rotation watches."""

    _counter = 0

    def __init__(self) -> None:
        type(self)._counter += 1
        self.session_id = f"session_{type(self)._counter:03d}"
        self.dropped_message_count = 0
        self.lost_events = 0
        self.recorded: list[dict] = []
        self.finalized = False
        self.finalize_reason = ""

    def record(self, event: dict) -> None:
        if self.finalized:
            raise RuntimeError("cannot record after finalization")
        self.recorded.append(event)

    def record_control_event(self, event: dict) -> None:
        self.recorded.append(event)
        if "dropped_message_count" in event:
            self.dropped_message_count = max(
                self.dropped_message_count,
                int(event["dropped_message_count"]),
            )

    def finalize(self, *, clean_shutdown: bool, reason: str | None = None) -> None:
        self.finalized = True
        self.finalize_reason = reason or ""


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _rotator(clock: _Clock, **kwargs):  # noqa: ANN202
    rotated: list[object] = []
    damage: list[int] = []
    wrapper = RotatingRecorder(
        _FakeRecorder,
        on_rotated_out=rotated.append,
        on_damage=damage.append,
        healthy_seconds=30.0,
        min_session_seconds=120.0,
        clock=clock,
        **kwargs,
    )
    return wrapper, rotated, damage


def test_an_intact_session_is_never_rotated_away() -> None:
    clock = _Clock()
    wrapper, rotated, _ = _rotator(clock)
    for i in range(100):
        clock.now += 10.0  # far beyond every window - but no damage ever
        wrapper.record({"i": i})
    assert rotated == []
    assert wrapper.rotations == 0


def test_first_damage_notifies_paper_immediately_with_the_exact_count() -> None:
    """Bridge drops mean the analysis tape already has holes - tell paper NOW."""
    clock = _Clock()
    wrapper, _, damage = _rotator(clock)
    wrapper.record({"i": 1})
    wrapper._inner.dropped_message_count = 17  # noqa: SLF001 - simulate bridge drops
    wrapper.record({"i": 2})
    assert damage == [17], "paper must learn of the hole at the first drop"
    assert wrapper.session_damaged is True
    assert "17 event(s) lost" in wrapper.first_drop_reason


def test_growing_damage_reports_deltas_not_totals() -> None:
    clock = _Clock()
    wrapper, _, damage = _rotator(clock)
    wrapper._inner.dropped_message_count = 10  # noqa: SLF001
    wrapper.record({"i": 1})
    wrapper._inner.dropped_message_count = 25  # noqa: SLF001
    wrapper.record({"i": 2})
    assert damage == [10, 15]


def test_rotation_waits_for_a_full_quiet_window() -> None:
    """Rotation must not happen while damage is still accumulating."""
    clock = _Clock()
    wrapper, rotated, _ = _rotator(clock)
    clock.now += 200.0  # past min_session_seconds
    wrapper._inner.dropped_message_count = 5  # noqa: SLF001
    wrapper.record({"i": 1})  # damage observed here
    clock.now += 10.0
    wrapper.record({"i": 2})  # only 10s quiet - too soon
    assert wrapper.rotations == 0
    clock.now += 25.0  # 35s quiet in total
    wrapper.record({"i": 3})
    assert wrapper.rotations == 1
    assert len(rotated) == 1


def test_the_damaged_session_finalizes_unclean_with_the_reason() -> None:
    clock = _Clock()
    wrapper, rotated, _ = _rotator(clock)
    clock.now += 200.0
    wrapper._inner.dropped_message_count = 43_296  # noqa: SLF001
    wrapper.record({"i": 1})
    clock.now += 31.0
    wrapper.record({"i": 2})
    old = rotated[0]
    assert old.finalized is True
    assert "rotated away after data loss" in old.finalize_reason
    assert "43296 event(s)" in old.finalize_reason
    # Its loss accounting is untouched: nothing was hidden or reset.
    assert old.dropped_message_count == 43_296


def test_the_rotation_boundary_loses_no_event() -> None:
    """The triggering event becomes the FIRST event of the new session."""
    clock = _Clock()
    wrapper, rotated, _ = _rotator(clock)
    clock.now += 200.0
    wrapper._inner.dropped_message_count = 1  # noqa: SLF001
    wrapper.record({"i": "last-into-damaged"})
    clock.now += 31.0
    wrapper.record({"i": "first-into-clean"})
    old = rotated[0]
    assert {"i": "last-into-damaged"} in old.recorded
    assert {"i": "first-into-clean"} not in old.recorded
    assert wrapper._inner.recorded == [{"i": "first-into-clean"}]  # noqa: SLF001


def test_the_fresh_session_starts_clean_and_can_rotate_again() -> None:
    clock = _Clock()
    wrapper, _, _ = _rotator(clock)
    clock.now += 200.0
    wrapper._inner.dropped_message_count = 3  # noqa: SLF001
    wrapper.record({"i": 1})
    clock.now += 31.0
    wrapper.record({"i": 2})
    assert wrapper.session_damaged is False, "the new session must start with a clean slate"
    assert wrapper.first_drop_reason == ""
    # A second damage episode in the new session rotates again (after windows).
    clock.now += 200.0
    wrapper._inner.dropped_message_count = 7  # noqa: SLF001
    wrapper.record({"i": 3})
    clock.now += 31.0
    wrapper.record({"i": 4})
    assert wrapper.rotations == 2


def test_lifetime_bridge_drop_count_does_not_poison_the_fresh_session() -> None:
    """The Java lifetime high-water mark is baselined per connection/session."""
    clock = _Clock()
    wrapper, _, damage = _rotator(clock)
    wrapper.record_control_event({
        "type": "connected",
        "dropped_message_count": 1_031_435,
    })
    wrapper.record({"i": "clean despite historical lifetime count"})
    assert wrapper.session_damaged is False
    assert damage == []

    wrapper.record_control_event({
        "type": "heartbeat",
        "dropped_message_count": 1_031_437,
    })
    assert wrapper.session_damaged is True
    assert damage == [2]


def test_rotated_session_baselines_the_same_lifetime_counter() -> None:
    clock = _Clock()
    wrapper, _, damage = _rotator(clock)
    wrapper.record_control_event({"type": "connected", "dropped_message_count": 100})
    clock.now += 200.0
    wrapper.record_control_event({"type": "heartbeat", "dropped_message_count": 101})
    clock.now += 31.0
    wrapper.record({"i": "first clean event"})
    assert wrapper.rotations == 1
    assert wrapper.session_damaged is False
    wrapper.record_control_event({"type": "heartbeat", "dropped_message_count": 101})
    assert wrapper.session_damaged is False
    assert damage == [1]


def test_min_session_seconds_prevents_flapping() -> None:
    """A feed that drops every minute must not shred sessions every minute."""
    clock = _Clock()
    wrapper, _, _ = _rotator(clock)
    wrapper._inner.dropped_message_count = 1  # noqa: SLF001
    wrapper.record({"i": 1})  # damage at t~1000, session started t=1000
    clock.now += 60.0  # quiet 60s > healthy 30s, but session only 60s old
    wrapper.record({"i": 2})
    assert wrapper.rotations == 0, "min session age must gate rotation"


def test_session_damage_reads_pipeline_and_recorder_counters() -> None:
    class _WithMetrics(_FakeRecorder):
        class metrics:  # noqa: N801 - mimic StageMetrics attribute
            overflow = 4

    recorder = _WithMetrics()
    recorder.dropped_message_count = 2
    recorder.lost_events = 1
    assert session_damage(recorder) == 7


def test_launcher_wires_rotation_with_paper_notification() -> None:
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "RotatingRecorder(" in source
    assert "on_damage=(" in source and "_analysis_damage" in source, (
        "paper must learn of bridge drops the moment they are observed"
    )
    assert "on_rotated_out=lambda old: _finalize_assistant_session(" in source, (
        "a rotated-out session must still produce its reports"
    )


def test_pipeline_holder_sees_metrics_through_the_rotating_wrapper(tmp_path) -> None:
    """Regression: isinstance(RecorderPipeline) silently dropped the wrapper.

    That lost recorder metrics AND the capture-priority pressure signal (the
    analysis feed could no longer see recorder occupancy). Found by soak
    metrics showing recorder occupancy None mid-run.
    """
    from app.database.recorder import MarketSessionRecorder
    from app.market.bounded_pipeline import PipelineStateHolder, RecorderPipeline

    def make():  # noqa: ANN202
        return RecorderPipeline(MarketSessionRecorder(root_dir=tmp_path))

    wrapper = RotatingRecorder(make)
    holder = PipelineStateHolder()
    holder.attach(object(), wrapper)  # exactly how on_connection_started calls it
    snap = holder.snapshot()
    assert snap.recorder, "recorder metrics must survive the rotating wrapper"
    assert "capacity" in snap.recorder and int(snap.recorder["capacity"]) > 0
    # The pressure gauge must see the recorder again.
    assert holder.worst_queue_occupancy_fraction() >= 0.0
