"""User-controlled paper-engine options read from production_config.yaml.

Same discipline as the LIVE gate: generated code never writes this file, a
missing file or key means the conservative default, and an unreadable file
fails closed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path


def read_momentum_enabled(production_config_path: Path) -> bool:
    """Whether the optional momentum setup runs beside the canonical plan.

    Missing file or missing key means FALSE (canonical order-flow plan only).
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
    return payload.get("paper_momentum_setup_enabled") is True


def read_bidirectional_lab_enabled(production_config_path: Path) -> bool:
    """Whether the backend runs the READ-ONLY Bidirectional Paper Lab live tap.

    Missing/unreadable/missing-key means FALSE (the live stream is untouched).
    The tap only observes events and publishes its own isolated paper state; it
    never routes an order or changes runtime/strategy behaviour.
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
    return payload.get("paper_bidirectional_lab_live_enabled") is True


def read_autonomous_enabled(production_config_path: Path) -> bool:
    """Whether the backend proposes shadow-only autonomous research candidates.

    Missing/unreadable/missing-key means FALSE. Proposal is bounded, shadow-only,
    and never touches runtime, risk, or the broker.
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
    return payload.get("autonomous_enabled") is True


def read_autonomous_training_enabled(production_config_path: Path) -> bool:
    """Whether the backend runs HEAVY offline model training in the autonomous cycle.

    Missing/unreadable/missing-key means FALSE. Training rebuilds datasets from
    raw and must never compete with capture, so it is opt-in.
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
    return payload.get("autonomous_training_enabled") is True


def read_ml_decision_policy_enabled(production_config_path: Path) -> bool:
    """Whether the paper engine's decisions may be vetoed by the shadow model.

    Missing file or missing key means FALSE (heuristic-only decisions, the
    unchanged behaviour). See config/production_config.yaml for the full
    policy description.
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
    return payload.get("paper_ml_decision_policy_enabled") is True


def read_strategy_profile(production_config_path: Path) -> str:
    """Return the paper strategy profile: 'canonical' (default) or 'relaxed'.

    Any missing/unknown/unreadable value falls back to 'canonical' - the honest
    strategy is never disabled by accident.
    """
    from app.strategy.profiles import CANONICAL, normalize_profile

    if not production_config_path.is_file():
        return CANONICAL
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return CANONICAL
    if not isinstance(payload, dict):
        return CANONICAL
    return normalize_profile(payload.get("paper_strategy_profile"))


def read_instrument(production_config_path: Path) -> str:
    """Return the paper instrument: 'MNQ' (default) or 'NQ'.

    Missing/unknown/unreadable falls back to MNQ (the micro contract).
    """
    from app.instruments import DEFAULT_INSTRUMENT, resolve_instrument

    if not production_config_path.is_file():
        return DEFAULT_INSTRUMENT
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return DEFAULT_INSTRUMENT
    if not isinstance(payload, dict):
        return DEFAULT_INSTRUMENT
    return resolve_instrument(payload.get("paper_instrument")).symbol


_STOP_KEYS = (
    "paper_break_even_trigger_ticks",
    "paper_break_even_lock_ticks",
    "paper_trail_activation_ticks",
    "paper_trail_distance_ticks",
)


def read_stop_settings(production_config_path: Path) -> dict[str, Decimal]:
    """Return the dynamic-stop tick settings; all 0 (disabled) if unset/unreadable."""
    from decimal import InvalidOperation

    zeros = {key.replace("paper_", ""): Decimal("0") for key in _STOP_KEYS}
    if not production_config_path.is_file():
        return zeros
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return zeros
    if not isinstance(payload, dict):
        return zeros
    result = dict(zeros)
    for key in _STOP_KEYS:
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if value >= 0:
            result[key.replace("paper_", "")] = value
    return result


def read_fixed_sizing(production_config_path: Path) -> dict[str, object]:
    """Return fixed_contracts / max_risk_per_trade_usd (both 0 = disabled).

    The two settings are atomic: fixed sizing is enabled only when both keys are
    present and valid. Missing/unreadable config, a missing key, or any invalid
    value fails closed to the disabled defaults, so malformed configuration can
    neither activate fixed sizing nor prevent the paper engine from starting.
    """
    from decimal import InvalidOperation

    defaults: dict[str, object] = {
        "fixed_contracts": 0,
        "max_risk_per_trade_usd": Decimal("0"),
    }
    if not production_config_path.is_file():
        return defaults
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return defaults
    if not isinstance(payload, dict):
        return defaults

    raw_contracts = payload.get("paper_fixed_contracts")
    raw_risk = payload.get("paper_max_risk_per_trade_usd")
    if raw_contracts is None or raw_risk is None or isinstance(raw_contracts, bool):
        return defaults
    try:
        contracts = int(raw_contracts)
        risk = Decimal(str(raw_risk))
    except (InvalidOperation, TypeError, ValueError):
        return defaults
    if contracts <= 0 or not risk.is_finite() or risk <= 0:
        return defaults
    return {
        "fixed_contracts": contracts,
        "max_risk_per_trade_usd": risk,
    }


def read_min_reward_risk(production_config_path: Path) -> Decimal:
    """Return the minimum reward:risk a setup must offer (default 0 = disabled).

    Fail-closed: any missing/invalid/negative value disables the filter (0) rather
    than accidentally blocking every trade.
    """
    from decimal import InvalidOperation

    if not production_config_path.is_file():
        return Decimal("0")
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config fails closed to disabled
        return Decimal("0")
    if not isinstance(payload, dict):
        return Decimal("0")
    raw = payload.get("paper_min_reward_risk")
    if raw is None or isinstance(raw, bool):
        return Decimal("0")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not value.is_finite() or value < 0:
        return Decimal("0")
    return value


def read_target_reward_risk(production_config_path: Path) -> Decimal:
    """Return the target-widening reward:risk multiple (default 0 = disabled).

    Fail-closed: any missing/invalid/negative value leaves the strategy's own
    target unchanged (0), never widens it by accident.
    """
    from decimal import InvalidOperation

    if not production_config_path.is_file():
        return Decimal("0")
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config fails closed to disabled
        return Decimal("0")
    if not isinstance(payload, dict):
        return Decimal("0")
    raw = payload.get("paper_target_reward_risk")
    if raw is None or isinstance(raw, bool):
        return Decimal("0")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not value.is_finite() or value < 0:
        return Decimal("0")
    return value


_ENTRY_DEFAULTS: dict[str, object] = {
    "entry_order_type": "market",
    "entry_limit_offset_ticks": Decimal("0"),
    "entry_limit_timeout_seconds": Decimal("0"),
    "entry_limit_cancel_ticks": Decimal("0"),
    "entry_require_trade_through": False,
}


def read_entry_settings(production_config_path: Path) -> dict[str, object]:
    """Return the entry-order-type settings (default: market/taker).

    ``paper_entry_order_type: limit`` enables passive maker scalping - rest at the
    near touch, fill at that price, cancel unfilled. Fail-closed: unreadable
    config, an unknown type, or a limit request with NO timeout and NO cancel
    distance all fall back to market, so a config typo can neither block the paper
    engine nor leave a resting order able to stall it forever.
    """
    from decimal import InvalidOperation

    defaults = dict(_ENTRY_DEFAULTS)
    if not production_config_path.is_file():
        return defaults
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return defaults
    if not isinstance(payload, dict):
        return defaults
    if str(payload.get("paper_entry_order_type", "market")).strip().lower() != "limit":
        return defaults  # market, or any unknown value -> taker default

    def _non_negative(key: str) -> Decimal:
        raw = payload.get(key)
        if raw is None or isinstance(raw, bool):
            return Decimal("0")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        return value if value.is_finite() and value >= 0 else Decimal("0")

    timeout = _non_negative("paper_entry_limit_timeout_seconds")
    cancel = _non_negative("paper_entry_limit_cancel_ticks")
    if timeout <= 0 and cancel <= 0:
        # A limit with no cap could block every future entry; refuse to enable it.
        return defaults
    return {
        "entry_order_type": "limit",
        "entry_limit_offset_ticks": _non_negative("paper_entry_limit_offset_ticks"),
        "entry_limit_timeout_seconds": timeout,
        "entry_limit_cancel_ticks": cancel,
        "entry_require_trade_through": payload.get("paper_entry_require_trade_through") is True,
    }


def read_scalping_cadence(production_config_path: Path) -> dict[str, Decimal]:
    """Return entry cooldown / time-stop in seconds (defaults 300 / 900).

    Fail-closed to the swing-safe defaults: any missing/invalid value keeps the
    default rather than accidentally removing the cooldown or the time cap. A
    zero cooldown IS honoured (back-to-back scalps); the time stop stays positive.
    """
    from decimal import InvalidOperation

    defaults = {"entry_cooldown_seconds": Decimal("300"), "time_stop_seconds": Decimal("900")}
    if not production_config_path.is_file():
        return defaults
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return defaults
    if not isinstance(payload, dict):
        return defaults

    def _finite(key: str) -> Decimal | None:
        raw = payload.get(key)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return value if value.is_finite() else None

    result = dict(defaults)
    cooldown = _finite("paper_entry_cooldown_seconds")
    if cooldown is not None and cooldown >= 0:
        result["entry_cooldown_seconds"] = cooldown
    time_stop = _finite("paper_time_stop_seconds")
    if time_stop is not None and time_stop > 0:
        result["time_stop_seconds"] = time_stop
    return result


def read_daily_limits(production_config_path: Path) -> dict[str, int]:
    """Return max_entries_per_day / max_losses_per_day (defaults 3, fail-safe)."""
    defaults = {"max_entries_per_day": 3, "max_losses_per_day": 3}
    if not production_config_path.is_file():
        return defaults
    try:
        import yaml

        payload = yaml.safe_load(production_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config must fail closed
        return defaults
    if not isinstance(payload, dict):
        return defaults
    result = dict(defaults)
    for key, out in (("paper_max_entries_per_day", "max_entries_per_day"),
                     ("paper_max_losses_per_day", "max_losses_per_day")):
        raw = payload.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:  # 0 = unlimited (learning stage)
            result[out] = value
    return result
