"""The "Download from Databento" card of the import window, driven through its real widgets.

Databento is the in-memory fake from ``test_databento_download`` (no network, no money), and the
key store points at a temporary APPDATA, so the user's real key is never read or touched.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import app.gui.databento_import_window as module
from app.gui.databento_import_window import DatabentoImportWindow
from app.importers.databento import imported_days
from app.importers.databento_api import HistoricalClient
from app.importers.databento_download import download_folder
from tests.test_databento_download import KEY, PRICE_PER_DAY, RTH_PRICE, FakeDatabento, daily_files, http_error

WAIT_MS = 60_000


@pytest.fixture(autouse=True)
def _private_key_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    monkeypatch.setattr(module, "latest_available_day", lambda now=None: date(2025, 7, 21))


def make(qtbot: object, tmp_path: Path, fake: FakeDatabento, *, with_key: bool = True,
         history_index: int = 1, whole_day: bool = True) -> DatabentoImportWindow:
    window = DatabentoImportWindow(out_root=tmp_path / "raw")
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.page.client_factory = lambda key: HistoricalClient(key, opener=fake)
    if with_key:
        assert window.page.set_api_key(KEY)
    window.page.history_combo.setCurrentIndex(history_index)          # 5 trading days back from Mon 2025-07-21
    window.page.hours_combo.setCurrentIndex(1 if whole_day else 0)    # whole days: 2025-07-15..21
    return window


def idle(qtbot: object, window: DatabentoImportWindow) -> None:
    qtbot.waitUntil(lambda: not window.page.running, timeout=WAIT_MS)  # type: ignore[attr-defined]


def test_the_card_sits_between_add_files_and_progress(qtbot, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path, FakeDatabento(), with_key=False)
    layout = window.page.content_layout
    names = [layout.itemAt(i).widget().objectName() for i in range(layout.count()) if layout.itemAt(i).widget()]
    assert names.index("import_drop_card") < names.index("import_download_card") < names.index("import_progress_card")


def test_without_a_key_nothing_can_be_priced_or_bought(qtbot, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path, FakeDatabento(), with_key=False)
    page = window.page
    assert "not set" in page.key_status.text()
    assert not page.price_button.isEnabled() and not page.buy_button.isEnabled()
    assert not page.forget_key_button.isEnabled()


def test_a_saved_key_is_masked_kept_in_app_data_and_can_be_forgotten(qtbot, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path, FakeDatabento(), with_key=False)
    page = window.page
    assert not page.set_api_key("not-a-key")
    assert "Key not saved" in page.key_status.text()
    assert page.set_api_key(KEY)
    assert KEY not in page.key_status.text() and "db-...cdef" in page.key_status.text()
    assert (tmp_path / "appdata" / "MNQ Intelligence" / "databento_api_key.txt").read_text().strip() == KEY
    assert page.price_button.isEnabled() and not page.buy_button.isEnabled()   # a price comes first
    page.forget_key()
    assert "not set" in page.key_status.text() and not page.price_button.isEnabled()


def test_checking_the_price_buys_nothing_and_arms_the_buy_button(qtbot, tmp_path: Path) -> None:
    fake = FakeDatabento()
    window = make(qtbot, tmp_path, fake)
    page = window.page
    page.check_price()
    idle(qtbot, window)
    assert "7 day(s) to buy for $2.94" in page.price_line.text()
    assert page.buy_button.isEnabled() and page.buy_button.text() == "Buy and import ($2.94)"
    assert fake.bought() == []
    page.history_combo.setCurrentIndex(2)                                  # another history: price is stale
    assert not page.buy_button.isEnabled() and "check the price again" in page.price_line.text()


def test_declining_the_confirmation_buys_nothing(qtbot, tmp_path: Path) -> None:
    fake = FakeDatabento()
    window = make(qtbot, tmp_path, fake)
    page = window.page
    asked: list[str] = []
    page.confirm_purchase = lambda title, question: asked.append(question) or False
    page.check_price()
    idle(qtbot, window)
    page.buy()
    assert asked and "$2.94" in asked[0] and fake.bought() == []
    assert page.status_line.text() == "Not bought."


def test_buy_download_and_import_end_to_end_then_the_downloads_are_cleaned_up(qtbot, tmp_path: Path) -> None:
    fake = FakeDatabento(daily_files(tmp_path, days=7))
    fake.polls_until_done = 0
    window = make(qtbot, tmp_path, fake)
    page = window.page
    page.confirm_purchase = lambda title, question: True
    page.check_price()
    idle(qtbot, window)
    page.buy()
    qtbot.waitUntil(lambda: not page.running and page.state_badge.text() in {  # type: ignore[attr-defined]
        "FINISHED", "FINISHED WITH PROBLEMS", "FAILED", "NOT DOWNLOADED"}, timeout=WAIT_MS)
    assert page.state_badge.text() in {"FINISHED", "FINISHED WITH PROBLEMS"}, page.status_line.text()
    assert len(fake.bought()) == 1
    assert len(imported_days(tmp_path / "raw")) == 7
    assert not list(download_folder(tmp_path / "raw").rglob("*.csv.zst"))   # imported, so deleted
    page.check_price()                                                     # the same week again
    idle(qtbot, window)
    assert "already imported" in page.price_line.text() and not page.buy_button.isEnabled()
    assert len(fake.bought()) == 1


def test_a_rejected_key_is_explained_and_nothing_is_bought(qtbot, tmp_path: Path) -> None:
    fake = FakeDatabento()
    fake.fail_with = http_error(401, "Invalid API key")
    window = make(qtbot, tmp_path, fake)
    page = window.page
    page.check_price()
    idle(qtbot, window)
    assert page.state_badge.text() == "NO PRICE"
    assert "did not accept the API key" in page.price_line.text() and KEY not in page.price_line.text()
    assert not page.buy_button.isEnabled() and fake.bought() == []


def test_stop_ends_a_price_check_and_the_drop_area_says_what_is_running(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import app.importers.databento_download as downloads

    monkeypatch.setattr(downloads, "imported_days", lambda root: {"2025-07-18"})   # two runs to price
    fake = FakeDatabento()
    entered, release = threading.Event(), threading.Event()

    def slow(request, timeout):
        entered.set()
        release.wait(10)
        return fake(request, timeout)

    window = make(qtbot, tmp_path, fake)
    page = window.page
    page.client_factory = lambda key: HistoricalClient(key, opener=slow)
    page.check_price()
    qtbot.waitUntil(entered.is_set, timeout=WAIT_MS)
    assert page.stop_button.isEnabled() and "Checking the price" in page.drop_area.title.text()
    page.stop_import()
    release.set()
    idle(qtbot, window)
    assert page.state_badge.text() == "STOPPED"                       # a choice, not a failure
    assert page.status_line.text() == "Price check stopped. Nothing was bought."
    assert fake.bought() == []


def test_a_purchase_that_delivers_no_data_files_says_so_honestly(qtbot, tmp_path: Path) -> None:
    class NoData(FakeDatabento):
        def listing(self, job_id):
            return [item for item in super().listing(job_id) if item["filename"] == "metadata.json"]

    fake = NoData()
    fake.polls_until_done = 0
    window = make(qtbot, tmp_path, fake)
    page = window.page
    page.confirm_purchase = lambda title, question: True
    page.check_price()
    idle(qtbot, window)
    page.buy()
    idle(qtbot, window)
    assert page.state_badge.text() == "NO DATA" and "no data files" in page.status_line.text()
    assert "already imported" not in page.status_line.text()
    assert page.price_line.text() == "Check the price to see what this would cost."


def test_the_new_card_writes_no_colours_of_its_own() -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "setStyleSheet(" not in source.split("class DatabentoImportWindow")[0]
    assert "#" not in "".join(line for line in source.splitlines() if "import_download" in line)
    assert D(str(PRICE_PER_DAY)) * 7 == D("2.94")


def test_trading_hours_are_the_default_and_change_the_price_and_the_question(qtbot, tmp_path: Path) -> None:
    fresh = DatabentoImportWindow(out_root=tmp_path / "other")
    qtbot.addWidget(fresh)
    assert fresh.page.hours_combo.currentData() is True                   # the cheaper choice by default
    assert fresh.page.history_combo.currentData() == 5
    fake = FakeDatabento()
    window = make(qtbot, tmp_path, fake, whole_day=False)
    page = window.page
    asked: list[str] = []
    page.confirm_purchase = lambda title, question: asked.append(question) or False
    page.check_price()
    idle(qtbot, window)
    assert "trading hours only" in page.price_line.text() and "5 day(s) to buy for $0.85" in page.price_line.text()
    assert RTH_PRICE * 5 == D("0.85")
    page.buy()
    assert "trading hours (09:30–16:50 New York)" in asked[0] and fake.bought() == []
    page.hours_combo.setCurrentIndex(1)                                   # whole day: the price is stale
    assert not page.buy_button.isEnabled() and "check the price again" in page.price_line.text()


class _SecondDayCorrupt(FakeDatabento):
    """Day two arrives with a wrong checksum: the run stops after day one."""

    def listing(self, job_id: str) -> list[dict[str, object]]:
        items = super().listing(job_id)
        for item in items:
            if "20250716" in str(item["filename"]):
                item["hash"] = "sha256:" + "0" * 64
        return items


def test_days_are_imported_as_they_land_so_a_later_failure_keeps_them(qtbot, tmp_path: Path) -> None:
    fake = _SecondDayCorrupt(daily_files(tmp_path, days=7))
    fake.polls_until_done = 0
    window = make(qtbot, tmp_path, fake, whole_day=False)     # trading hours: one purchase per day
    page = window.page
    page.confirm_purchase = lambda title, question: True
    page.check_price()
    idle(qtbot, window)
    page.buy()
    qtbot.waitUntil(lambda: not page.running and page.state_badge.text() in {  # type: ignore[attr-defined]
        "FINISHED", "FINISHED WITH PROBLEMS", "FAILED", "NOT DOWNLOADED"}, timeout=WAIT_MS)
    assert page.state_badge.text() == "NOT DOWNLOADED"
    assert "checksum" in page.status_line.text() and "1 day(s) were imported before it stopped" in page.status_line.text()
    assert imported_days(tmp_path / "raw") == {"2025-07-15"}                 # day one is in, not waiting
    assert not list(download_folder(tmp_path / "raw").rglob("*.csv.zst"))    # and its file is deleted
    assert "2025-07-15" in " ".join(" ".join(row) for row in page._table_rows())
