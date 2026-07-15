"""STAGE 1 regression tests: honest attribution of bridge queue-drop counts.

The Java forwarding queue reports a *cumulative* overflow count that lives for
as long as the Bookmap add-on stays loaded - across Python restarts and
reconnects. These tests pin the fix that stops that stale lifetime number from
being presented as the current connection's loss (the ``dropped 86211`` shown
next to ``recording no`` in the reported screenshot).
"""

from __future__ import annotations

from app.runtime.controller import AutomaticRuntimeController
from app.runtime.health import HealthMonitor


def test_stale_lifetime_drops_not_attributed_to_unconnected_session() -> None:
    """A heartbeat carrying a huge cumulative count before any connect is 0 for the session."""
    monitor = HealthMonitor()
    # Bridge heartbeat arrives (add-on loaded from a previous run) but no
    # 'connected' handshake has happened this run.
    monitor.mark_dropped_messages(86211)
    assert monitor.dropped_message_count == 86211  # lifetime high-water is preserved
    assert monitor.current_session_dropped_message_count == 0  # but not blamed on this run
    snapshot = monitor.snapshot()
    assert snapshot.dropped_message_count == 86211
    assert snapshot.current_session_dropped_message_count == 0


def test_connect_baselines_current_session_drops() -> None:
    """After connect, only drops beyond the connect-time baseline count as current."""
    monitor = HealthMonitor()
    monitor.mark_dropped_messages(86211)  # pre-connect stale cumulative
    monitor.mark_bookmap_connected()
    assert monitor.current_session_dropped_message_count == 0
    monitor.mark_dropped_messages(86215)  # 4 fresh drops during this connection
    assert monitor.current_session_dropped_message_count == 4
    assert monitor.dropped_message_count == 86215


def test_reconnect_rebaselines_current_session() -> None:
    """Each connection restarts the current-session delta from its own baseline."""
    monitor = HealthMonitor()
    monitor.mark_bookmap_connected()  # baseline 0
    monitor.mark_dropped_messages(10)
    assert monitor.current_session_dropped_message_count == 10
    monitor.mark_bookmap_disconnected("socket closed")
    monitor.mark_bookmap_connected()  # new connection rebaselines at 10
    assert monitor.current_session_dropped_message_count == 0
    monitor.mark_dropped_messages(12)  # 2 fresh drops on the new connection
    assert monitor.current_session_dropped_message_count == 2
    assert monitor.dropped_message_count == 12  # lifetime high-water unaffected


def test_lifetime_drops_never_regress_below_high_water() -> None:
    """If the add-on restarts and reports a lower cumulative, the lifetime high-water holds."""
    monitor = HealthMonitor()
    monitor.mark_dropped_messages(500)
    monitor.mark_dropped_messages(3)  # add-on reloaded, counter reset in Java
    assert monitor.dropped_message_count == 500  # high-water preserved
    assert monitor.latest_reported_dropped == 3  # latest raw value tracked separately


def test_controller_snapshot_separates_current_and_lifetime_drops() -> None:
    """The GUI-facing snapshot exposes both figures; a pre-connect heartbeat is 0 current."""
    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    controller.handle_control_event({"type": "heartbeat", "timestamp_ns": 1, "dropped_message_count": 86211})
    snapshot = controller.snapshot()
    assert snapshot.dropped_message_count == 86211
    assert snapshot.current_session_dropped_message_count == 0


def test_controller_counts_drops_after_connect() -> None:
    """Drops reported after a real connect are attributed to the current session."""
    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    controller.handle_control_event({"type": "heartbeat", "timestamp_ns": 1, "dropped_message_count": 86211})
    controller.handle_control_event({"type": "connected", "timestamp_ns": 2, "dropped_message_count": 86211})
    controller.handle_control_event({"type": "data_gap", "timestamp_ns": 3, "dropped_message_count": 86214, "reason": "overflow"})
    snapshot = controller.snapshot()
    assert snapshot.current_session_dropped_message_count == 3
    assert snapshot.dropped_message_count == 86214
