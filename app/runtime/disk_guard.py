"""Disk-space and recorder-throughput guards for unattended capture.

Two slow-motion failure modes that a multi-day run WILL eventually meet:

* the output disk fills up - Parquet flushes start failing long after the GUI
  stopped being watched;
* the writer falls behind a sustained ingress rate - queues absorb it until
  they cannot, and by then the session is already damaged.

Both guards are cheap polls (no per-event cost), surfaced in the status
snapshot, and act in the documented order: reduce optional work FIRST (pause
research), degrade visibility work next, and only ever block recording when
safe persistence genuinely cannot be guaranteed. Staging cannot fix a full
disk - that is stated, not papered over.
"""

from __future__ import annotations

import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DISK_HEALTHY = "HEALTHY"
DISK_WARNING = "WARNING"
DISK_CRITICAL = "CRITICAL"

DEFAULT_WARNING_FREE_GB = 5.0
DEFAULT_CRITICAL_FREE_GB = 1.0
# Sustained writer deficit: persisted rate below ingress rate for this many
# consecutive checks before we call it real pressure (bursts are normal).
PRESSURE_CONSECUTIVE_CHECKS = 6


@dataclass(frozen=True, slots=True)
class DiskState:
    """One free-space measurement with its classified level."""

    level: str
    free_gb: float
    total_gb: float
    detail: str


class DiskGuard:
    """Classify free space under the recording root."""

    def __init__(
        self,
        path: Path | str,
        *,
        warning_free_gb: float = DEFAULT_WARNING_FREE_GB,
        critical_free_gb: float = DEFAULT_CRITICAL_FREE_GB,
        probe: Callable[[Path], tuple[int, int, int]] | None = None,
    ) -> None:
        """Guard the volume containing ``path`` (created if missing)."""
        self._path = Path(path)
        self._warning = warning_free_gb
        self._critical = critical_free_gb
        self._probe = probe or (lambda p: tuple(shutil.disk_usage(p)))  # type: ignore[return-value]

    def check(self) -> DiskState:
        """Measure and classify the volume's free space."""
        target = self._path if self._path.exists() else self._path.parent
        try:
            total, _used, free = self._probe(target)
        except OSError as error:
            return DiskState(DISK_CRITICAL, 0.0, 0.0,
                             f"disk probe failed: {type(error).__name__}: {error}")
        free_gb = free / (1024 ** 3)
        total_gb = total / (1024 ** 3)
        if free_gb < self._critical:
            return DiskState(
                DISK_CRITICAL, free_gb, total_gb,
                f"{free_gb:.2f} GB free < critical {self._critical:.1f} GB - "
                "safe persistence cannot be guaranteed; recording integrity is at risk",
            )
        if free_gb < self._warning:
            return DiskState(
                DISK_WARNING, free_gb, total_gb,
                f"{free_gb:.2f} GB free < warning {self._warning:.1f} GB - "
                "research paused; free disk space soon",
            )
        return DiskState(DISK_HEALTHY, free_gb, total_gb, f"{free_gb:.1f} GB free")


class ThroughputGuard:
    """Detect a writer that is SUSTAINEDLY slower than ingress."""

    def __init__(self, *, consecutive_checks: int = PRESSURE_CONSECUTIVE_CHECKS) -> None:
        """Create an idle guard; feed it (ingress, persisted) totals per check."""
        self._needed = consecutive_checks
        self._history: deque[bool] = deque(maxlen=consecutive_checks)
        self._last: tuple[int, int] | None = None
        self.last_deficit_per_second = 0.0
        self._last_time = time.monotonic()

    def observe(self, ingress_total: int, persisted_total: int) -> bool:
        """Record one sample; True when pressure has been sustained."""
        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_time)
        self._last_time = now
        if self._last is None:
            self._last = (ingress_total, persisted_total)
            return False
        ingress_delta = ingress_total - self._last[0]
        persisted_delta = persisted_total - self._last[1]
        self._last = (ingress_total, persisted_total)
        # Pressure = events genuinely arriving AND the writer falling behind.
        behind = ingress_delta > 0 and persisted_delta < ingress_delta
        self.last_deficit_per_second = max(0.0, (ingress_delta - persisted_delta) / elapsed)
        self._history.append(behind)
        return len(self._history) == self._needed and all(self._history)

    @property
    def under_pressure(self) -> bool:
        """Whether the most recent full window was all-behind."""
        return len(self._history) == self._needed and all(self._history)
