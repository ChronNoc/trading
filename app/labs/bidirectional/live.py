"""Read-only live tap: drive the lab engine from the backend's market stream.

Runs INSIDE the backend as a read-only sink on the analysis feed. It observes
the ``(event, market_state)`` tuples the backend already produces, converts each
to the lab's immutable :class:`MarketEvent`, and drives a :class:`LabEngine` at
full tick resolution. It publishes lab state to a runtime file for the GUI to
read, and re-reads its activation config from a runtime file the GUI writes.

It never writes to the feed, the recorder, the strategy, or any runtime state,
and imports no execution code (enforced by the lab's safety tests). If anything
in here raises, it is swallowed - the experiment must never disturb capture.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.labs.bidirectional.config import (
    AccountConfig,
    ActivationSpec,
    BreakEvenConfig,
    TrailConfig,
)
from app.labs.bidirectional.engine import LabEngine
from app.labs.bidirectional.market import MarketEvent
from app.labs.bidirectional.statistics import compute_statistics

CONFIG_NAME = "bidirectional_lab_config.json"
STATE_NAME = "bidirectional_lab_state.json"
_PUBLISH_INTERVAL_SECONDS = 0.5


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class LabConfigFile:
    """The GUI -> backend activation config for the live lab (paper-only)."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.path = Path(runtime_dir) / CONFIG_NAME

    def write(self, config: dict) -> None:
        _atomic_write_json(self.path, config)

    def read(self) -> dict | None:
        return _read_json(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class LabStateFile:
    """The backend -> GUI published lab state for the live lab."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.path = Path(runtime_dir) / STATE_NAME

    def write(self, state: dict) -> None:
        _atomic_write_json(self.path, state)

    def read(self) -> dict | None:
        return _read_json(self.path)


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def market_event_from_stream(event: object, state: object, seq: int = 0) -> MarketEvent:
    """Convert a backend ``(event, MarketState)`` pair into an immutable MarketEvent."""
    last: Decimal | None = None
    kind = "depth"
    if isinstance(event, dict) and "timestamp_ns" in event and "sequence_id" in event:
        kind = "trade"
        try:
            last = Decimal(str(event.get("price")))
        except (InvalidOperation, TypeError):
            last = None
    ts = int(getattr(state, "timestamp_ns", 0) or 0)
    return MarketEvent(ts_ns=ts, seq=seq, last=last,
                       bid=getattr(state, "best_bid", None), ask=getattr(state, "best_ask", None),
                       kind=kind)


class LabLiveRunner:
    """Feeds a LabEngine from the live stream, config-driven and state-publishing."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self._config = LabConfigFile(runtime_dir)
        self._state = LabStateFile(runtime_dir)
        self._lock = threading.Lock()
        self._engine: LabEngine | None = None
        self._enabled = False
        self._config_sig: object = None
        self._last_publish = 0.0
        self._last_config_check = 0.0

    def observe(self, event: object, state: object) -> None:
        """Read-only feed sink: drive the lab from one live market event."""
        try:
            now = time.monotonic()
            if now - self._last_config_check >= 0.5:
                self._last_config_check = now
                self._maybe_reload()
            if not self._enabled or self._engine is None:
                return
            with self._lock:
                self._engine.on_event(market_event_from_stream(event, state))
            if now - self._last_publish >= _PUBLISH_INTERVAL_SECONDS:
                self._last_publish = now
                self._publish()
        except Exception:  # noqa: BLE001 - the experiment must never disturb capture
            return

    def _maybe_reload(self) -> None:
        try:
            mtime = self._config.path.stat().st_mtime if self._config.path.is_file() else None
        except OSError:
            mtime = None
        if mtime == self._config_sig:
            return
        self._config_sig = mtime
        config = self._config.read()
        if not config or not config.get("enabled"):
            self._enabled = False
            return
        with self._lock:
            self._engine = self._build_engine(config)
            self._enabled = True
        self._publish()

    def _build_engine(self, config: dict) -> LabEngine:
        account = AccountConfig(
            starting_balance=_dec(config.get("starting_balance", "100000")),
            tick_size=_dec(config.get("tick_size", "0.25")),
            tick_value=_dec(config.get("tick_value", "0.50")),
            commission_per_contract=_dec(config.get("commission", "0.62")),
            entry_slippage_ticks=_dec(config.get("entry_slip", "0")),
            exit_slippage_ticks=_dec(config.get("exit_slip", "0")),
            stop_slippage_ticks=_dec(config.get("stop_slip", "1")),
            max_gross_contracts=int(config.get("max_gross_contracts", 1_000_000)),
        )
        engine = LabEngine(account)
        be = BreakEvenConfig(enabled=_dec(config.get("be_trigger")) > 0,
                             trigger_ticks=_dec(config.get("be_trigger")),
                             offset_ticks=_dec(config.get("be_offset")))
        tr = TrailConfig(enabled=_dec(config.get("trail_dist")) > 0,
                         activation_ticks=_dec(config.get("trail_act")),
                         distance_ticks=_dec(config.get("trail_dist")))
        one_shot = bool(config.get("one_shot", False))
        stop_ticks = _dec(config.get("stop_ticks", "2"))
        for i, price in enumerate(config.get("levels", [])):
            engine.arm(ActivationSpec(
                activation_id=str(i + 1), price=_dec(price),
                long_qty=int(config.get("long", 100)), short_qty=int(config.get("short", 100)),
                stop_ticks=stop_ticks, break_even=be, trailing=tr, one_shot=one_shot,
                max_activations=1 if one_shot else 1_000_000,
                require_leave_reenter=True, leave_distance_ticks=stop_ticks * 2,
            ))
        return engine

    def _publish(self) -> None:
        if self._engine is None:
            return
        engine = self._engine
        stats = compute_statistics(engine)
        payload = {
            "updated_unix": time.time(),
            "enabled": self._enabled,
            "stats": stats,
            "levels": [
                {"id": st.spec.activation_id, "price": str(st.spec.price), "status": st.status,
                 "activations": st.activations, "setups": len(st.setup_ids)}
                for st in engine.levels.values()
            ],
            "log_tail": [f"{e.ts_ns}  {e.text}" for e in engine.log[-400:]],
        }
        self._state.write(payload)
