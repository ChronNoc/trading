"""User-controlled paper-engine options read from production_config.yaml.

Same discipline as the LIVE gate: generated code never writes this file, a
missing file or key means the conservative default, and an unreadable file
fails closed.
"""

from __future__ import annotations

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


def read_stop_settings(production_config_path: Path) -> dict[str, "Decimal"]:
    """Return the dynamic-stop tick settings; all 0 (disabled) if unset/unreadable."""
    from decimal import Decimal, InvalidOperation

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
