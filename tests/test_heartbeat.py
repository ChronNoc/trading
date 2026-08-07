"""The liveness heartbeat must stay fresh independently of the snapshot build.

The bug this guards: under real-time load the backend's main loop could not
rebuild+write the full status snapshot within the staleness window, so a healthy
recording backend looked dead and was needlessly restarted. The heartbeat is now
published by its own thread from a cheap cached snapshot, so liveness (the beat)
is decoupled from freshness (the snapshot).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.runtime.heartbeat import HeartbeatWriter, SnapshotCache
from app.runtime.process_files import ProcessIdentity, StatusFile


def _identity() -> ProcessIdentity:
    return ProcessIdentity.current(config_fingerprint="fp")


def test_snapshot_cache_returns_the_latest_value() -> None:
    cache = SnapshotCache("first")
    assert cache.get() == "first"
    cache.set("second")
    cache.set("third")
    assert cache.get() == "third"


def test_heartbeat_rejects_a_nonpositive_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HeartbeatWriter(StatusFile(tmp_path), _identity(), lambda: "s", interval_seconds=0)


def test_heartbeat_publishes_running_status_from_the_provider(tmp_path: Path) -> None:
    status = StatusFile(tmp_path)
    cache = SnapshotCache("<snap-1>")
    hb = HeartbeatWriter(status, _identity(), cache.get, interval_seconds=0.05)
    hb.start()
    try:
        time.sleep(0.2)
    finally:
        hb.stop()
    doc = status.read()
    assert doc is not None
    assert doc["state"] == "RUNNING"
    assert doc["snapshot"] == "<snap-1>"
    assert doc["config_fingerprint"] == "fp"


def test_heartbeat_stays_fresh_even_when_the_snapshot_is_never_rebuilt(tmp_path: Path) -> None:
    """The decisive case: the main loop is too busy to refresh the snapshot, yet
    the supervisor must still see a fresh heartbeat and a live backend."""
    status = StatusFile(tmp_path)
    frozen = SnapshotCache("<built once, never refreshed>")
    hb = HeartbeatWriter(status, _identity(), frozen.get, interval_seconds=0.05)
    hb.start()
    try:
        first = float(status.read()["heartbeat_unix"])
        time.sleep(0.3)
        later = float(status.read()["heartbeat_unix"])
    finally:
        hb.stop()
    assert later > first  # fresh beats kept coming despite the frozen snapshot
    assert status.read()["snapshot"] == "<built once, never refreshed>"
    # This is what the supervisor checks - it must see the backend as alive.
    assert status.backend_alive(expected_fingerprint="fp") is True


def test_heartbeat_stop_halts_further_writes(tmp_path: Path) -> None:
    status = StatusFile(tmp_path)
    hb = HeartbeatWriter(status, _identity(), lambda: "s", interval_seconds=0.05)
    hb.start()
    time.sleep(0.15)
    hb.stop()
    frozen_beat = float(status.read()["heartbeat_unix"])
    time.sleep(0.2)
    assert float(status.read()["heartbeat_unix"]) == frozen_beat  # no beats after stop
    hb.stop()  # idempotent


def test_heartbeat_survives_a_failing_status_write(tmp_path: Path) -> None:
    """A failed write must be counted and logged, never crash the beat thread."""

    class _ThrowingSink:
        def write(self, snapshot_json: str, *, identity: object, state: str) -> None:
            raise OSError("simulated status write failure")

    hb = HeartbeatWriter(_ThrowingSink(), _identity(), lambda: "s", interval_seconds=0.05)
    assert hb.beat_once() is False
    assert hb.consecutive_failures == 1
    assert hb.beat_once() is False
    assert hb.consecutive_failures == 2
    # The background loop must keep running through failures, not die.
    hb.start()
    try:
        time.sleep(0.15)
    finally:
        hb.stop()
    assert hb.consecutive_failures >= 3
