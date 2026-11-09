"""``python -m tools.databento_download``: price only by default, buys only with --buy and a ceiling."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import tools.databento_download as cli
from app.importers.databento import imported_days
from app.importers.databento_api import HistoricalClient
from tests.test_databento_download import FakeDatabento, daily_files


@pytest.fixture(autouse=True)
def _yesterday_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "latest_available_day", lambda now=None: date(2025, 7, 17))


def run(fake: FakeDatabento, tmp_path: Path, *args: str) -> int:
    return cli.main([*args, "--out", str(tmp_path / "raw")], client=HistoricalClient("db-TestKey1234567890abcdef",
                                                                                     opener=fake))


def test_without_buy_it_only_prices(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeDatabento()
    assert run(fake, tmp_path, "--days", "3") == 0
    out = capsys.readouterr().out
    assert "trading hours only: 3 day(s) to buy for $0.51" in out and "nothing was bought" in out
    assert fake.bought() == []


def test_buy_needs_a_ceiling(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(FakeDatabento(), tmp_path, "--days", "3", "--buy")


def test_a_price_above_the_ceiling_buys_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeDatabento()
    assert run(fake, tmp_path, "--days", "3", "--buy", "--max-cost", "0.10") == 1
    assert "above your --max-cost" in capsys.readouterr().err and fake.bought() == []


def test_buy_within_the_ceiling_downloads_and_imports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeDatabento(daily_files(tmp_path))
    fake.polls_until_done = 0
    assert run(fake, tmp_path, "--start", "2025-07-15", "--end", "2025-07-17", "--hours", "full",
               "--buy", "--max-cost", "5") == 0
    assert imported_days(tmp_path / "raw") == {"2025-07-15", "2025-07-16", "2025-07-17"}
    assert "Imported 3 session(s)." in capsys.readouterr().out and len(fake.bought()) == 1
