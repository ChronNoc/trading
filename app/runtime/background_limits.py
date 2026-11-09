"""Keep heavy offline work slow and gentle so it can never overload the PC.

On 2026-11-09 the PC powered off hard (Kernel-Power 41, no bug check) while research used one
worker process per logical CPU (15 of 16) to rebuild every session, beside a training run and a
Databento import: hours of full load. Offline work (research rebuilds, training, imports) has no
deadline, so it now runs with a small fixed number of worker processes, each with one compute
thread, at below-normal priority: the desktop and the live recording always come first, and the
CPU stays far from full load. It takes longer; it gives the same results.
"""

from __future__ import annotations

import os
import sys

# Worker processes for background research, whatever the CPU size (an explicit worker count or the
# high-performance switch, both user choices, still override this).
BACKGROUND_MAX_WORKERS = 2
# Compute threads per background process (numeric libraries and Arrow would otherwise start one
# per logical CPU in EVERY process).
BACKGROUND_THREADS = 1
_THREAD_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS",
)
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000   # Windows SetPriorityClass


def background_worker_cap(cpu_count: int | None) -> int:
    """How many background worker processes to run on a machine with ``cpu_count`` logical CPUs."""
    return max(1, min(BACKGROUND_MAX_WORKERS, (cpu_count or 1) - 1))


def apply_background_limits(threads: int = BACKGROUND_THREADS, *, lower_priority: bool = True) -> list[str]:
    """Limit THIS process: ``threads`` compute threads and below-normal priority. Returns what it set.

    Call it first thing in a background process (a research worker, a training run, an import
    tool), never in the recording backend itself. Every step is best effort: a library that is not
    installed, or a platform call that is refused, leaves the rest in place.
    """
    threads = max(1, int(threads))
    applied: list[str] = []
    for name in _THREAD_VARIABLES:        # libraries loaded after this read them at start-up
        os.environ[name] = str(threads)
    applied.append(f"{threads} compute thread(s)")
    try:                                  # libraries already loaded: limit their pools now
        from threadpoolctl import threadpool_limits

        threadpool_limits(limits=threads)
    except Exception:  # noqa: BLE001 - optional
        pass
    try:
        import pyarrow

        pyarrow.set_cpu_count(threads)
        pyarrow.set_io_thread_count(max(1, threads))
        applied.append(f"Arrow {threads} thread(s)")
    except Exception:  # noqa: BLE001 - optional
        pass
    if lower_priority and _lower_priority():
        applied.append("below-normal priority")
    return applied


def _lower_priority() -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE   # 64-bit pseudo-handle, never truncated
            kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS))
        os.nice(10)
        return True
    except Exception:  # noqa: BLE001 - best effort
        return False


def research_worker_initializer() -> None:
    """``ProcessPoolExecutor(initializer=...)`` for background research workers."""
    apply_background_limits()
