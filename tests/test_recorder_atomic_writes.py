"""Atomic writes must not collide between concurrent writers.

The defect these guard is real and was measured, not theorised: 31 of 200
recorded sessions were destroyed by

    PermissionError: [WinError 32] The process cannot access the file because it
    is being used by another process: '...\\session_manifest.json.tmp'
    PermissionError: [Errno 13] Permission denied: '...\\session_manifest.json.tmp'

The cause was a FIXED temp filename. Two overlapping writers opened the same
``session_manifest.json.tmp``; whichever called ``replace`` first hit it while
the other still held a handle. Windows raises; POSIX silently interleaves two
payloads into one file, which is worse because it looks like success.

Every test writes to pytest's ``tmp_path``, so no real recording is touched.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.database.recorder import atomic_write_text, unique_temp_path


def test_temp_paths_are_unique_per_writer() -> None:
    """Two temp paths for one destination must never be the same file."""
    destination = Path("data/raw/session/session_manifest.json")
    first = unique_temp_path(destination, ".tmp")
    second = unique_temp_path(destination, ".tmp")
    assert first != second, "a fixed temp name is exactly the bug"
    assert first.parent == destination.parent, "temp must sit beside the target (same volume)"
    assert first.name.startswith(destination.name)


def test_atomic_write_publishes_the_payload(tmp_path: Path) -> None:
    target = tmp_path / "session_manifest.json"
    atomic_write_text(target, '{"ok": true}\n')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    """A stranded .tmp would be mistaken for a real artefact by the catalog."""
    target = tmp_path / "session_manifest.json"
    atomic_write_text(target, "{}\n")
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"stranded temp files: {leftovers}"


def test_concurrent_writers_never_collide_and_leave_valid_json(tmp_path: Path) -> None:
    """The exact race that destroyed 31 sessions.

    Many threads hammer the same manifest path. Every write must succeed, and the
    final file must be complete valid JSON from exactly one writer - never a
    torn interleave of two payloads.
    """
    target = tmp_path / "session_manifest.json"
    errors: list[BaseException] = []
    barrier = threading.Barrier(12)

    def write(index: int) -> None:
        payload = json.dumps({"writer": index, "padding": "x" * 5000}) + "\n"
        try:
            barrier.wait(timeout=10)  # maximise overlap
            for _ in range(15):
                atomic_write_text(target, payload)
        except BaseException as error:  # noqa: BLE001 - the test IS the assertion
            errors.append(error)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], f"concurrent writes must not raise: {errors[:3]}"
    document = json.loads(target.read_text(encoding="utf-8"))  # must not be torn
    assert document["padding"] == "x" * 5000, "the published file must be one writer's whole payload"
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"stranded temp files after the race: {leftovers}"


def test_a_failed_write_does_not_destroy_the_previous_file(tmp_path: Path) -> None:
    """A crash mid-write must leave the last good manifest intact."""
    target = tmp_path / "session_manifest.json"
    atomic_write_text(target, '{"generation": 1}\n')
    try:
        atomic_write_text(target, None)  # type: ignore[arg-type] - forced failure
    except Exception:  # noqa: BLE001 - expected
        pass
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_recorder_manifest_survives_concurrent_flush_and_finalize(tmp_path: Path) -> None:
    """End-to-end: the real recorder, written from several threads at once."""
    from app.database.recorder import MarketSessionRecorder

    recorder = MarketSessionRecorder(root_dir=tmp_path)
    errors: list[BaseException] = []

    def rewrite() -> None:
        try:
            for _ in range(20):
                recorder._write_manifest()  # noqa: SLF001 - the racing call site
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=rewrite) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], f"the manifest race must be gone: {errors[:3]}"
    # Must be complete valid JSON, not a torn interleave of two writers.
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["receiver_version"], "the published manifest must be whole"
    assert "event_counts" in manifest
    strays = [p.name for p in recorder.session_dir.iterdir() if ".tmp" in p.name]
    assert strays == [], f"stranded temp files: {strays}"


# --- Windows refuses renames transiently; retrying is the only cure ---------------


def test_replace_retries_through_a_transient_permission_error(tmp_path: Path, monkeypatch) -> None:
    """Windows raises WinError 5 while an AV scanner holds the destination.

    No lock can prevent that - the other handle is not our process - so the
    rename must retry. Measured: 16 threads rewriting one manifest produced 1-2
    such failures per run before this existed, and zero after.
    """
    import app.database.recorder as recorder_module

    source, target = tmp_path / "a.tmp", tmp_path / "a.json"
    source.write_text("payload", encoding="utf-8")
    calls = {"n": 0}
    real_replace = recorder_module.os.replace

    def flaky_replace(src, dst):  # noqa: ANN001, ANN202 - test double
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice, like a scanner holding the file
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(recorder_module.os, "replace", flaky_replace)
    recorder_module.replace_with_retry(source, target, initial_delay=0.001)
    assert calls["n"] == 3, "it must retry rather than give up on the first refusal"
    assert target.read_text(encoding="utf-8") == "payload"


def test_replace_gives_up_and_raises_on_a_persistent_permission_error(
    tmp_path: Path, monkeypatch,
) -> None:
    """A real permission problem must surface, not be retried into silence."""
    import app.database.recorder as recorder_module

    source, target = tmp_path / "a.tmp", tmp_path / "a.json"
    source.write_text("payload", encoding="utf-8")

    def always_denied(src, dst):  # noqa: ANN001, ANN202 - test double
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(recorder_module.os, "replace", always_denied)
    with pytest.raises(PermissionError):
        recorder_module.replace_with_retry(source, target, attempts=3, initial_delay=0.001)


def test_replace_does_not_retry_unrelated_errors(tmp_path: Path, monkeypatch) -> None:
    """Only handle contention is transient; a missing file must fail fast."""
    import app.database.recorder as recorder_module

    calls = {"n": 0}

    def missing(src, dst):  # noqa: ANN001, ANN202 - test double
        calls["n"] += 1
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(recorder_module.os, "replace", missing)
    with pytest.raises(FileNotFoundError):
        recorder_module.replace_with_retry(tmp_path / "a.tmp", tmp_path / "a.json")
    assert calls["n"] == 1, "a non-transient error must not be retried"
