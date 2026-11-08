"""Open the Databento drop-in window.

    .venv\\Scripts\\pythonw.exe -m tools.databento_import_app [FILE_OR_FOLDER ...]

Files, folders or .zip files given on the command line - for example dropped onto the desktop
shortcut - are imported straight away. The same window also takes files dropped onto it, or
chosen with its buttons. Every day in a file becomes a recorded session under ``data/raw``.

It is meant to run windowless through ``pythonw.exe``, so a startup failure is shown in a
native message box (the same one the main launcher uses), never silently.

    --smoke   open the window without showing it on screen, then close it (proves it starts)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # `py tools/databento_import_app.py` run as a script
    sys.path.insert(0, str(_ROOT))

from tools.launch_desktop import repo_root, show_error  # noqa: E402

ERROR_TITLE = "The Databento import window could not start"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="files, folders or .zip files to import at once")
    parser.add_argument("--out", type=Path, default=Path("data/raw"), help="recordings folder (default data/raw)")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--smoke", action="store_true", help="open without showing, then close (a start-up check)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Resolve what was given BEFORE changing directory, so a relative path still means what it said.
    paths = [path.resolve() for path in args.paths]
    root = repo_root()
    out_root = args.out if args.out.is_absolute() else root / args.out
    os.chdir(root)
    if args.smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication

        from app.gui.databento_import_window import DatabentoImportWindow
    except Exception as error:  # noqa: BLE001 - a broken environment must say so
        show_error(
            ERROR_TITLE,
            f"The application could not be loaded (check the .venv Python environment):\n\n"
            f"{type(error).__name__}: {error}",
        )
        return 2
    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = DatabentoImportWindow(out_root=out_root, initial_paths=paths, theme=args.theme)
        if args.smoke:
            app.processEvents()
            window.close()
            print("Databento import window: opened and closed cleanly.")
            return 0
        window.show()
        return int(app.exec())
    except Exception as error:  # noqa: BLE001 - last resort; never fail silently
        show_error(ERROR_TITLE, f"{type(error).__name__}: {error}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
