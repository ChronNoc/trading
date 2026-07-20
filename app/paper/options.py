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
