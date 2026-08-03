# Desktop Application, Icon, and One-Click Shortcut

This document describes the one-click Windows desktop experience for the MNQ
Intelligence assistant: the application icon, the launcher, and the shortcut
installer. It builds on the **existing** startup architecture
(`tools/start_assistant.py` + `tools/backend_supervisor.py`) rather than adding
a parallel launch path.

## Safety invariants (unchanged)

- **Delayed-paper mode only.** The launcher passes `--delayed-data-minutes 15`
  and no live/broker/arming flags. Delayed data structurally cannot reach a
  broker; LIVE stays locked (`live_enabled: false`).
- **No duplicate backends.** `start_assistant`/`backend_supervisor` detect a
  running backend via the singleton lock and attach rather than spawn a second.
- **Never fails silently.** Because the shortcut runs windowless (`pythonw`),
  any startup failure is shown in a native Windows message box naming the log
  directory (`logs/`).

## Components

### 1. Application icon — `assets/branding/mnq_intelligence.ico`

Original glass "intelligence terminal" mark (deep navy tile, thin cyan luminous
border, ascending cyan→violet bars, an emerald rising line + glowing node).
Rendered with Qt (no Pillow dependency) and packed into a multi-resolution
`.ico` (16 / 32 / 48 / 128 / 256, 32-bit). A 256px PNG source is kept alongside.

Regenerate:

```bash
.venv\Scripts\python.exe -m tools.generate_app_icon
```

Sources committed: `assets/branding/mnq_intelligence.ico`,
`assets/branding/mnq_intelligence_256.png`.

### 2. Launcher — `tools/launch_desktop.py`

A thin wrapper that resolves the repo root, sets the working directory, and
delegates to `tools.start_assistant.main(["--delayed-data-minutes", "15"])`.
It adds only startup-failure surfacing: `AssistantStartupError` and any
unexpected exception become a native error dialog pointing at `logs/`, so a
double-click launch is never a silent no-op. Run windowless via `pythonw.exe`.

### 3. Shortcut installer — `tools/install_desktop_shortcut.ps1`

Creates (or updates) a Desktop shortcut, optionally a Start Menu shortcut,
pointing at `.venv\Scripts\pythonw.exe -m tools.launch_desktop` with the repo as
the working directory and the `.ico` as the icon.

```powershell
# Create the desktop shortcut (no admin required):
powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1

# Also add a Start Menu entry:
powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1 -StartMenu

# Remove the shortcut(s):
powershell -ExecutionPolicy Bypass -File tools\install_desktop_shortcut.ps1 -Uninstall
```

Properties: no administrator privileges (writes only to the user's Desktop and
Start Menu), idempotent (re-running updates the shortcut in place), quoted paths
throughout, validates prerequisites (refuses to create a shortcut if `pythonw`
or the icon is missing), reads the target back to confirm success, and supports
clean uninstall.

## How to launch

Double-click **MNQ Intelligence** on the Desktop (or Start Menu). It starts the
persistent backend if needed, waits for readiness, and opens the GUI attached to
the authoritative runtime snapshot — in delayed-paper mode. Works after a normal
Windows restart. To connect real data, open Bookmap with the add-on and
subscribe to the MNQ contract.

## Packaging note (evaluated, not adopted)

A frozen `.exe` (PyInstaller/Nuitka) was considered and **not** adopted: the app
already launches reliably from the repo venv, and freezing a PySide6 + native
add-on stack adds fragile packaging for no functional gain. The shortcut +
`pythonw` approach is the simplest reliable option supported by the existing
architecture. If a self-contained installer is later required, PyInstaller is
the recommended starting point, driven from `tools/launch_desktop.py` as the
entry point.

## Tests

`tests/test_desktop_launcher.py` verifies: the launcher delegates to the real
`start_assistant` entry in delayed mode; it never passes a live/broker flag;
startup failures surface an error and return non-zero; the `.ico` is a valid
multi-resolution icon; and the installer script is safe, idempotent, and
uninstallable.
