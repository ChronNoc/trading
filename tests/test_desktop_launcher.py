"""Desktop launcher, app icon, and shortcut installer: safety + correctness."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import tools.launch_desktop as launch_desktop

REPO_ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = REPO_ROOT / "assets" / "branding" / "mnq_intelligence.ico"
PS1_PATH = REPO_ROOT / "tools" / "install_desktop_shortcut.ps1"


@pytest.fixture(autouse=True)
def _restore_cwd(monkeypatch: pytest.MonkeyPatch):
    import os

    original = os.getcwd()
    yield
    os.chdir(original)


def test_launcher_delegates_to_existing_start_assistant_in_delayed_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    import tools.start_assistant as start_assistant

    monkeypatch.setattr(start_assistant, "main", lambda argv=None: calls.append(list(argv)) or 0)
    code = launch_desktop.main(None)
    assert code == 0
    assert calls == [["--delayed-data-minutes", "15"]], "uses the existing entry, delayed mode"


def test_launcher_never_passes_a_live_or_broker_flag() -> None:
    # The launcher's fixed args must never enable live/broker/demo execution.
    joined = " ".join(launch_desktop.DEFAULT_ARGS).lower()
    for forbidden in ("live", "broker", "arm", "demo", "real"):
        assert forbidden not in joined
    source = (REPO_ROOT / "tools" / "launch_desktop.py").read_text(encoding="utf-8").lower()
    assert "--live" not in source and "--arm" not in source


def test_launcher_forwards_explicit_args(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    import tools.start_assistant as start_assistant

    monkeypatch.setattr(start_assistant, "main", lambda argv=None: seen.append(list(argv)) or 0)
    launch_desktop.main(["--no-gui"])
    assert seen == [["--no-gui"]]


def test_startup_failure_shows_an_error_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.start_assistant as start_assistant

    def boom(argv=None):
        raise start_assistant.AssistantStartupError("no venv")

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(start_assistant, "main", boom)
    monkeypatch.setattr(launch_desktop, "show_error",
                        lambda title, message: shown.append((title, message)))
    code = launch_desktop.main(None)
    assert code == 2
    assert shown, "a startup failure must surface a visible error, never fail silently"
    assert "log" in shown[0][1].lower(), "the error names the log location"


def test_system_exit_from_the_app_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.start_assistant as start_assistant

    def exit_two(argv=None):
        raise SystemExit(2)

    monkeypatch.setattr(start_assistant, "main", exit_two)
    assert launch_desktop.main(None) == 2


def test_app_icon_is_a_valid_multi_resolution_ico() -> None:
    assert ICO_PATH.is_file(), "run: python -m tools.generate_app_icon"
    data = ICO_PATH.read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and image_type == 1, "must be a Windows icon container"
    sizes = set()
    for i in range(count):
        width, height, *_ = struct.unpack("<BBBBHHII", data[6 + 16 * i:6 + 16 * i + 16])
        sizes.add(width or 256)
    # Windows shortcuts/taskbar need these sizes to stay crisp.
    assert {16, 32, 48, 256} <= sizes, f"missing required icon sizes; got {sorted(sizes)}"


def test_installer_script_is_safe_idempotent_and_uninstallable() -> None:
    assert PS1_PATH.is_file()
    text = PS1_PATH.read_text(encoding="utf-8")
    # Targets the existing launcher via the venv's windowless python.
    assert "tools.launch_desktop" in text
    assert "pythonw.exe" in text
    assert "WorkingDirectory" in text and "IconLocation" in text
    # No-admin, idempotent, uninstallable, optional Start Menu, validated.
    assert "WScript.Shell" in text
    assert "Uninstall" in text and "StartMenu" in text
    assert "TargetPath" in text  # validation reads it back
    # Must not silently enable live trading anywhere.
    assert "live" not in text.lower() or "no live trading" in text.lower()
    # Never requires admin elevation.
    assert "RunAs" not in text and "requireAdministrator" not in text
