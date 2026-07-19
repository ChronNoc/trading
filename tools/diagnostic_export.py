"""One-click redacted diagnostic export: a bounded ZIP, secrets excluded.

    .venv\\Scripts\\python.exe -m tools.diagnostic_export [--out diag.zip]

Includes: recent (tail-bounded) logs, crash log, supervisor log, the current
status snapshot, runtime identity files, the LATEST session manifest, version
fingerprints, and a fresh self-test result. Excludes BY CONSTRUCTION:
credential values, environment variables, tokens, raw recordings, ledgers, and
anything under the user's data trees except the single latest manifest.
Credential VALUES found in included text are additionally scrubbed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Sequence

TAIL_BYTES = 200_000  # bound every included text file


def _redact(text: str) -> str:
    from app.execution.demo_service import CREDENTIAL_VARIABLES

    for name in CREDENTIAL_VARIABLES:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, f"<{name}>")
    return text


def _tail(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"<unreadable: {error}>"
    return _redact(data[-TAIL_BYTES:])


def build_export(out_path: Path, *, runtime_dir: Path, log_dir: Path,
                 raw_root: Path) -> Path:
    """Write the bounded, redacted ZIP and return its path."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.selftest import run_selftest

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("status.json", "binding.json", "supervisor.log", "backend.out",
                     "final_status_failure.txt"):
            path = runtime_dir / name
            if path.is_file():
                archive.writestr(f"runtime/{name}", _tail(path))
        for name in ("assistant.log", "crash.log"):
            path = log_dir / name
            if path.is_file():
                archive.writestr(f"logs/{name}", _tail(path))
        manifests = sorted(raw_root.rglob("session_manifest.json"),
                           key=lambda p: p.stat().st_mtime)
        if manifests:
            archive.writestr("latest_session_manifest.json", _tail(manifests[-1]))
        try:
            import subprocess

            commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                    text=True, timeout=10, check=False).stdout.strip()
        except Exception:  # noqa: BLE001
            commit = "unknown"
        archive.writestr("versions.json", json.dumps({
            "python": sys.version,
            "git_commit": commit,
            "exported_at_unix": time.time(),
        }, indent=2))
        selftest_rows = run_selftest(runtime_dir, raw_root)
        archive.writestr("selftest.txt", _redact("\n".join(
            f"{verdict:4s} {name}: {detail}" for name, verdict, detail in selftest_rows)))
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI wrapper."""
    parser = argparse.ArgumentParser(description="Redacted diagnostic export.")
    parser.add_argument("--out", type=Path,
                        default=Path("runtime") / f"diagnostic_{int(time.time())}.zip")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)
    path = build_export(args.out, runtime_dir=args.runtime_dir,
                        log_dir=args.log_dir, raw_root=args.raw_root)
    print(f"diagnostic export written: {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
