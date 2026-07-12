"""YAML-defined prototype scenarios so new market situations need no code.

A scenario file describes a sequence of windows (warm-up, absorption
setups, volatility transitions, disconnect drills). The loader builds a
:class:`PrototypeScenario` from it using the same deterministic event
builders as the default demonstration, so every scenario exercises the
identical receiver -> strategy -> risk pipeline.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from app.prototype.scenarios import (
    DEFAULT_SEED,
    DEFAULT_START_TIMESTAMP_NS,
    PrototypeScenario,
    PrototypeScheduledEvent,
    PrototypeWindow,
    SCENARIO_VERSION,
    _append_absorption,
    _append_depth,
    _append_disconnect_reconnect_controls,
    _append_volatility_transition,
    _append_warmup,
)

_WARMUP_DURATION_NS = 160_000_000_000
_ABSORPTION_WINDOW_NS = 80_000_000_000
_ABSORPTION_SLOT_NS = 120_000_000_000
_VOLATILITY_SLOT_NS = 140_000_000_000

_VALID_KINDS = ("warmup", "absorption", "volatility_transition", "disconnect_reconnect")


class ScenarioDefinitionError(ValueError):
    """Raised when a scenario YAML file is invalid."""


@dataclass(frozen=True, slots=True)
class ScenarioFile:
    """A discovered scenario file with its display name."""

    name: str
    path: Path


def available_scenarios(directory: Path) -> tuple[ScenarioFile, ...]:
    """List scenario YAML files in a directory, sorted by name."""
    if not directory.is_dir():
        return ()
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    return tuple(ScenarioFile(name=path.stem, path=path) for path in files)


def load_scenario_yaml(path: Path) -> PrototypeScenario:
    """Build a deterministic prototype scenario from a YAML definition.

    The definition must contain exactly one clean absorption window
    (``reload: true`` and ``reclaim: true``) and exactly one rejected
    lookalike (anything less), because the runtime evaluates and reports
    both. A session-end control block is always appended so recordings
    finalize cleanly.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ScenarioDefinitionError(f"cannot read scenario file {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ScenarioDefinitionError(f"scenario file {path} must contain a YAML mapping")

    seed = _read_int(raw, "seed", DEFAULT_SEED)
    start_timestamp_ns = _read_int(raw, "start_timestamp_ns", DEFAULT_START_TIMESTAMP_NS)
    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ScenarioDefinitionError(f"scenario file {path} needs a non-empty 'windows' list")

    rng = random.Random(seed)
    events: list[PrototypeScheduledEvent] = []
    cursor_ns = start_timestamp_ns
    sequence_id = 1
    clean_window: PrototypeWindow | None = None
    rejected_window: PrototypeWindow | None = None
    saw_disconnect = False
    # Warm-up walks +/-0.25 from 100.00 for 24 steps and volatility swings
    # around 100.50, so both stay inside this span; absorption levels extend it.
    touched_low = Decimal("93.00")
    touched_high = Decimal("107.00")

    for entry in windows_raw:
        if not isinstance(entry, dict) or "kind" not in entry:
            raise ScenarioDefinitionError(f"every window needs a 'kind'; got {entry!r}")
        kind = str(entry["kind"])
        if kind not in _VALID_KINDS:
            raise ScenarioDefinitionError(f"unknown window kind {kind!r}; expected one of {_VALID_KINDS}")

        if kind == "warmup":
            _append_warmup(events, cursor_ns, rng)
            cursor_ns += _WARMUP_DURATION_NS
        elif kind == "absorption":
            level = _read_decimal(entry, "level")
            reload_bids = bool(entry.get("reload", False))
            reclaim = bool(entry.get("reclaim", False))
            is_clean = reload_bids and reclaim
            scenario_kind = "clean_long_absorption_reclaim" if is_clean else "rejected_lookalike"
            touched_low = min(touched_low, level - Decimal("3.00"))
            touched_high = max(touched_high, level + Decimal("3.00"))
            _append_wide_book_clear(events, cursor_ns - 5_000_000_000, scenario_kind, touched_low, touched_high)
            sequence_id = _append_absorption(
                events,
                start_timestamp_ns=cursor_ns,
                important_level_price=level,
                include_reload=reload_bids,
                include_reclaim=reclaim,
                sequence_id=sequence_id,
                scenario=scenario_kind,
            )
            window = PrototypeWindow(
                kind=scenario_kind,
                start_timestamp_ns=cursor_ns,
                end_timestamp_ns=cursor_ns + _ABSORPTION_WINDOW_NS,
                important_level_price=level,
                description=str(entry.get("description", f"{scenario_kind} at {level}")),
            )
            if is_clean:
                if clean_window is not None:
                    raise ScenarioDefinitionError("scenario defines more than one clean absorption window")
                clean_window = window
            else:
                if rejected_window is not None:
                    raise ScenarioDefinitionError("scenario defines more than one rejected lookalike window")
                rejected_window = window
            cursor_ns += _ABSORPTION_SLOT_NS
        elif kind == "volatility_transition":
            sequence_id = _append_volatility_transition(events, cursor_ns, sequence_id)
            cursor_ns += _VOLATILITY_SLOT_NS
        else:
            _append_disconnect_reconnect_controls(events, cursor_ns, seed)
            cursor_ns += 40_000_000_000
            saw_disconnect = True

    if clean_window is None:
        raise ScenarioDefinitionError("scenario needs one absorption window with reload: true and reclaim: true")
    if rejected_window is None:
        raise ScenarioDefinitionError("scenario needs one absorption window that fails (reload/reclaim false)")
    if not saw_disconnect:
        _append_disconnect_reconnect_controls(events, cursor_ns, seed)

    return PrototypeScenario(
        events=tuple(sorted(events, key=lambda item: item.timestamp_ns)),
        clean_window=clean_window,
        rejected_window=rejected_window,
        seed=seed,
        scenario_version=SCENARIO_VERSION,
        start_timestamp_ns=start_timestamp_ns,
    )


def _append_wide_book_clear(
    events: list[PrototypeScheduledEvent],
    start_timestamp_ns: int,
    scenario_kind: str,
    low: Decimal,
    high: Decimal,
) -> None:
    """Zero every book level in the touched span so stale depth from earlier
    windows cannot distort the next window's best bid/ask."""
    price = low
    index = 0
    while price <= high:
        _append_depth(events, start_timestamp_ns + index, "bid", price, Decimal("0"), Decimal("0"), scenario_kind, "wide book clear")
        index += 1
        _append_depth(events, start_timestamp_ns + index, "ask", price, Decimal("0"), Decimal("0"), scenario_kind, "wide book clear")
        index += 1
        price += Decimal("0.25")


def _read_int(mapping: dict[str, object], key: str, default: int) -> int:
    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScenarioDefinitionError(f"'{key}' must be an integer, got {value!r}")
    return value


def _read_decimal(entry: dict[str, object], key: str) -> Decimal:
    value = entry.get(key)
    if value is None:
        raise ScenarioDefinitionError(f"absorption window needs a '{key}' price")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ScenarioDefinitionError(f"'{key}' is not a valid price: {value!r}") from error
