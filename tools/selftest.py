"""System self-test: check readiness without sending a single order.

    .venv\\Scripts\\python.exe -m tools.selftest [--runtime-dir runtime]

Read-only checks with named PASS/FAIL/WARN results and a nonzero exit when any
hard check fails. Credential checks report PRESENCE only - no value is read
into the output. Also invoked by the Diagnostics screen via the CLI, never on
the Qt thread.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def run_selftest(runtime_dir: Path, output_root: Path) -> list[tuple[str, str, str]]:
    """Return (name, verdict, detail) rows; import-light and side-effect free."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.execution.demo_service import credential_checklist
    from app.execution.live_gate import read_live_enabled
    from app.runtime.disk_guard import DISK_HEALTHY, DiskGuard
    from app.runtime.process_files import SingletonLock, StatusFile

    rows: list[tuple[str, str, str]] = []

    status = StatusFile(runtime_dir)
    alive = status.backend_alive()
    age = status.staleness_seconds()
    rows.append(("backend_heartbeat", PASS if alive else FAIL,
                 f"alive={alive} age={'n/a' if age is None else f'{age:.1f}s'} "
                 f"pid={SingletonLock(runtime_dir).holder()}"))

    binding_path = runtime_dir / "binding.json"
    if binding_path.is_file():
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            rows.append(("receiver_port", PASS, f"bound {binding.get('host')}:{binding.get('port')}"))
        except Exception as error:  # noqa: BLE001
            rows.append(("receiver_port", FAIL, f"binding.json unreadable: {error}"))
    else:
        rows.append(("receiver_port", FAIL if alive else WARN,
                     "no binding.json (backend not bound?)"))

    try:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".selftest_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rows.append(("output_writable", PASS, str(output_root)))
    except OSError as error:
        rows.append(("output_writable", FAIL, f"{type(error).__name__}: {error}"))

    disk = DiskGuard(output_root).check()
    rows.append(("disk_space", PASS if disk.level == DISK_HEALTHY else
                 (WARN if disk.level == "WARNING" else FAIL), disk.detail))

    checklist = credential_checklist()
    missing = [name for name, present in checklist if not present]
    rows.append(("tradovate_demo_credentials", PASS if not missing else WARN,
                 "all present" if not missing else f"missing: {', '.join(missing)}"))

    live = read_live_enabled(Path("config/production_config.yaml"))
    rows.append(("live_locked", PASS if live is False else FAIL,
                 "live_enabled is false" if live is False else "LIVE IS NOT LOCKED"))

    jar = Path("bookmap_addon_java/build/libs/mnq-bookmap-forwarder-all.jar")
    rows.append(("bookmap_jar", PASS if jar.is_file() else WARN,
                 f"{jar} ({jar.stat().st_size:,} bytes)" if jar.is_file() else "not built"))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """Print the table; nonzero exit when any FAIL exists."""
    parser = argparse.ArgumentParser(description="MNQ assistant self-test (no orders).")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--output-root", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)
    rows = run_selftest(args.runtime_dir, args.output_root)
    width = max(len(name) for name, _, _ in rows)
    for name, verdict, detail in rows:
        print(f"{verdict:4s}  {name.ljust(width)}  {detail}")
    return 0 if all(v != FAIL for _, v, _ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
