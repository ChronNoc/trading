"""Desktop launcher, app icon, and shortcut installer: safety + correctness."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import tools.launch_desktop as launch_desktop

REPO_ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = REPO_ROOT / "assets" / "branding" / "mnq_intelligence.ico"
IMPORTER_ICO_PATH = REPO_ROOT / "assets" / "branding" / "databento_import.ico"
PS1_PATH = REPO_ROOT / "tools" / "install_desktop_shortcut.ps1"


def _ico_sizes(data: bytes) -> set[int]:
    reserved, image_type, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and image_type == 1, "must be a Windows icon container"
    sizes = set()
    for i in range(count):
        width, height, *_ = struct.unpack("<BBBBHHII", data[6 + 16 * i:6 + 16 * i + 16])
        sizes.add(width or 256)
    return sizes


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


@pytest.mark.parametrize(
    ("ico_path", "command"),
    [
        (ICO_PATH, "python -m tools.generate_app_icon"),
        (IMPORTER_ICO_PATH, "python -m tools.generate_app_icon --importer"),
    ],
    ids=["app", "databento_import"],
)
def test_app_icon_is_a_valid_multi_resolution_ico(ico_path: Path, command: str) -> None:
    assert ico_path.is_file(), f"run: {command}"
    sizes = _ico_sizes(ico_path.read_bytes())
    # Windows shortcuts/taskbar need these sizes to stay crisp.
    assert {16, 32, 48, 256} <= sizes, f"missing required icon sizes; got {sorted(sizes)}"


def test_importer_icon_is_generated_distinct_from_the_app_icon(qapp: object, tmp_path: Path) -> None:
    # `qapp` (pytest-qt) makes the session's QApplication first, so generate() reuses it. Without
    # it, generate() would create a bare QGuiApplication and a later widget test would crash.
    from tools import generate_app_icon

    app_paths = generate_app_icon.generate(tmp_path / "app")
    importer_paths = generate_app_icon.generate(tmp_path / "importer", importer=True)
    assert app_paths["ico"].name == "mnq_intelligence.ico"
    assert importer_paths["ico"].name == "databento_import.ico"
    importer_ico = importer_paths["ico"].read_bytes()
    assert {16, 32, 48, 256} <= _ico_sizes(importer_ico)
    assert importer_ico != app_paths["ico"].read_bytes(), "the two shortcuts must be told apart"
    assert importer_paths["png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


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


def test_installer_can_also_place_the_databento_import_shortcut() -> None:
    text = PS1_PATH.read_text(encoding="utf-8")
    # An opt-in switch, so the default run still installs only the main app.
    assert "[switch]$Importer" in text
    assert "-m tools.databento_import_app" in text
    assert "databento_import.ico" in text
    assert "'Databento Import'" in text
    # The arguments are read back too, so a shortcut can never open the wrong app.
    assert "$check.Arguments -ne $Arguments" in text
    # Windows PowerShell 5.1 reads a BOM-less script as ANSI: keep it pure ASCII.
    assert text.isascii()
    # The window it opens exists and never routes orders.
    importer_source = (REPO_ROOT / "tools" / "databento_import_app.py").read_text(encoding="utf-8").lower()
    for forbidden in ("--live", "--arm", "broker"):
        assert forbidden not in importer_source
