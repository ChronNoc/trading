"""Background work is slow and gentle: a few one-thread, below-normal-priority processes."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from app.runtime.background_limits import BACKGROUND_MAX_WORKERS, background_worker_cap


@pytest.mark.parametrize(("cpus", "workers"), [(None, 1), (1, 1), (2, 1), (3, 2), (16, 2), (64, 2)])
def test_background_workers_are_a_small_fixed_number_whatever_the_cpu(cpus, workers: int) -> None:
    assert background_worker_cap(cpus) == workers <= BACKGROUND_MAX_WORKERS


def test_a_limited_process_gets_one_thread_and_below_normal_priority() -> None:
    """Run in a child so this test process keeps its own priority and threads."""
    script = textwrap.dedent("""
        import os, sys
        from app.runtime.background_limits import apply_background_limits
        applied = apply_background_limits()
        import pyarrow
        print(os.environ["OMP_NUM_THREADS"], pyarrow.cpu_count(), pyarrow.io_thread_count())
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32")
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.GetPriorityClass.argtypes = (wintypes.HANDLE,)
            k.GetPriorityClass.restype = wintypes.DWORD
            print(hex(k.GetPriorityClass(k.GetCurrentProcess())))
        else:
            print(os.nice(0))
        print(applied)
    """)
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True,
                         timeout=120).stdout.splitlines()
    assert out[0] == "1 1 1"
    if sys.platform == "win32":
        assert out[1] == "0x4000"            # BELOW_NORMAL_PRIORITY_CLASS
    else:
        assert int(out[1]) >= 10
    assert "below-normal priority" in out[2]


def test_research_and_rebuild_workers_start_with_the_limits() -> None:
    import inspect

    from app.research import build_orchestrator, research_service

    assert "initializer=research_worker_initializer" in inspect.getsource(research_service.ResearchService.run_batch)
    assert "initializer=research_worker_initializer" in inspect.getsource(build_orchestrator.schedule_pending_builds)
