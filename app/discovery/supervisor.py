"""Mode supervisor - the single owner of OBSERVE/LIVE state (item 32).

Workers receive a read-only view. There is deliberately no API anywhere
in this package that writes the live flag: going LIVE means a human edits
the protected production config themselves. This module only reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PRODUCTION_CONFIG = Path("config/production_config.yaml")


@dataclass(frozen=True, slots=True)
class ModeView:
    """Read-only view of the operating mode handed to workers."""

    mode: str
    live_armed: bool


class ModeSupervisor:
    """Reads the protected production config; never writes it."""

    def __init__(self, config_path: Path = DEFAULT_PRODUCTION_CONFIG) -> None:
        """Create a supervisor reading from the given config path."""
        self._config_path = Path(config_path)

    def view(self) -> ModeView:
        """Return the current mode. Missing config means OBSERVE, not LIVE."""
        live_armed = False
        if self._config_path.is_file():
            try:
                payload = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                payload = {}
            if isinstance(payload, dict):
                live_armed = payload.get("live_mode") is True
        return ModeView(mode="LIVE" if live_armed else "OBSERVE", live_armed=live_armed)
