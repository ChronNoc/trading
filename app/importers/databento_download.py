"""Plan, price and run a Databento download, then hand the files to the importer.

The money rules:

* nothing is bought until the user has seen the price and confirmed it;
* a run never spends more than the amount confirmed (every price is checked again just before
  buying, and nothing is bought if the total went up);
* no day is paid for twice: days already imported are left out, and every day bought is written
  to a small ledger (``jobs.json`` in the download folder) the moment the job is submitted, so a
  run that was stopped, timed out or crashed continues from the paid job instead of buying again.
  Only if Databento has expired that job is the day priced again (and confirmed again).

``plan_download`` prices exactly the days still needed; ``download_plan`` buys the new ones,
waits for Databento to prepare them, and downloads the daily CSV files (new and already paid).
Importing them is ``app.importers.databento``'s job, unchanged. Historical only: the live feed
stays Bookmap.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.importers.databento import imported_days
from app.importers.databento_api import DatabentoError, HistoricalClient, RangeRequest, RemoteFile

MAX_DAYS_PER_PLAN = 366
DATA_SUFFIX = ".csv.zst"
DOWNLOAD_FOLDER = ".databento_downloads"   # beside data/raw, never inside it
LEDGER_FILE = "jobs.json"
CENT = Decimal("0.01")
_LIVE_STATES = ("received", "queued", "processing", "done")


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    """What a download would fetch and cost."""

    first_day: date
    last_day: date                                   # inclusive
    runs: tuple[tuple[date, date], ...]              # (start, end exclusive): consecutive days to BUY
    costs: tuple[Decimal, ...]                       # Databento's price of each run, US dollars
    paid_days: tuple[tuple[str, str], ...]           # (day, job id): bought earlier, only to download
    already_imported: tuple[str, ...]                # days left out: already imported

    @property
    def total_cost(self) -> Decimal:
        """The price of what would be bought now (days already paid cost nothing)."""
        return sum(self.costs, Decimal("0"))

    @property
    def days_to_buy(self) -> int:
        """How many UTC days would be bought now."""
        return sum((end - start).days for start, end in self.runs)

    @property
    def nothing_to_do(self) -> bool:
        """Every day in the range is already imported."""
        return not self.runs and not self.paid_days


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    """Progress for the window and the command line."""

    stage: str            # pricing | submitting | waiting | downloading | done
    message: str
    done: int = 0
    total: int = 0


ProgressSink = Callable[[DownloadEvent], None]


def download_folder(out_root: Path) -> Path:
    """Where downloaded files wait to be imported: beside ``out_root``, never inside it."""
    return Path(out_root).parent / DOWNLOAD_FOLDER


def latest_available_day(now: datetime | None = None) -> date:
    """The last whole UTC day that can be bought (yesterday, UTC)."""
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).date() - timedelta(days=1)


def missing_runs(days: Iterable[date]) -> list[tuple[date, date]]:
    """Group days into runs of consecutive days, as (start, end exclusive)."""
    runs: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if runs and runs[-1][1] == day:
            runs[-1] = (runs[-1][0], day + timedelta(days=1))
        else:
            runs.append((day, day + timedelta(days=1)))
    return runs


# --- the ledger of days already bought -----------------------------------------------------------


def read_ledger(folder: Path) -> dict[str, str]:
    """Day (YYYY-MM-DD) -> the Databento job that bought it."""
    try:
        document = json.loads((Path(folder) / LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    days = document.get("days") if isinstance(document, dict) else None
    return {str(day): str(job) for day, job in days.items()} if isinstance(days, dict) else {}


def write_ledger(folder: Path, days: dict[str, str]) -> None:
    """Save the ledger atomically."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LEDGER_FILE
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "days": dict(sorted(days.items()))}, indent=1) + "\n",
                         encoding="utf-8")
    os.replace(temporary, target)


# --- plan -----------------------------------------------------------------------------------------


def plan_download(
    client: HistoricalClient, out_root: Path, first_day: date, last_day: date, *,
    now: datetime | None = None, progress: ProgressSink | None = None, cancel: Callable[[], bool] | None = None,
) -> DownloadPlan:
    """Price exactly the days of [first_day, last_day] still needed. Spends nothing.

    ``cancel`` is checked before every call to Databento, so a price check can be stopped.
    """
    if first_day > last_day:
        raise ValueError("The first day must not be after the last day.")
    latest = latest_available_day(now)
    if last_day > latest:
        raise ValueError(f"Databento history goes up to yesterday (UTC): the last day can be {latest} at most.")
    if (last_day - first_day).days + 1 > MAX_DAYS_PER_PLAN:
        raise ValueError(f"At most {MAX_DAYS_PER_PLAN} days at a time.")
    imported = imported_days(out_root)
    every_day = [first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)]
    needed = [day for day in every_day if day.isoformat() not in imported]
    ledger = read_ledger(download_folder(out_root))
    live_jobs: dict[str, bool] = {}
    paid: list[tuple[str, str]] = []
    to_buy: list[date] = []
    for day in needed:
        job = ledger.get(day.isoformat())
        if job is not None and job not in live_jobs:
            _stop_pricing(cancel)
            _emit(progress, "pricing", f"Checking the earlier Databento job {job}", 0, 0)
            live_jobs[job] = client.job_state(job) in _LIVE_STATES
        if job is not None and live_jobs[job]:
            paid.append((day.isoformat(), job))
        else:
            to_buy.append(day)
    runs = missing_runs(to_buy)
    costs: list[Decimal] = []
    for index, (start, end) in enumerate(runs, 1):
        _stop_pricing(cancel)
        _emit(progress, "pricing", f"Asking Databento for the price ({index} of {len(runs)})", index - 1, len(runs))
        costs.append(client.get_cost(RangeRequest(start, end)))
    already = tuple(day.isoformat() for day in every_day if day.isoformat() in imported)
    return DownloadPlan(first_day, last_day, tuple(runs), tuple(costs), tuple(paid), already)


# --- buy and download -----------------------------------------------------------------------------


def download_plan(
    client: HistoricalClient, plan: DownloadPlan, out_root: Path, *, confirmed_cost: Decimal,
    progress: ProgressSink | None = None, cancel: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 10.0, max_wait_seconds: float = 4 * 3600.0,
) -> list[Path]:
    """Buy the new days of ``plan``, download every needed day; returns the files to import.

    ``confirmed_cost`` is the price the user saw and accepted. The runs to buy are priced again
    first, and nothing at all is bought if their total is now above ``confirmed_cost`` (rounded up
    to the cent).
    """
    if plan.nothing_to_do:
        return []
    folder = download_folder(out_root)
    ceiling = confirmed_cost.quantize(CENT, rounding="ROUND_CEILING")
    fresh = sum((client.get_cost(RangeRequest(start, end)) for start, end in plan.runs), Decimal("0"))
    if fresh > ceiling:
        raise DatabentoError(
            f"The price changed to ${fresh:.2f}, above the ${ceiling:.2f} you confirmed. "
            "Nothing was bought; check the price again.")
    files: list[Path] = []
    # 1. days paid for earlier: download only
    by_job: dict[str, list[str]] = {}
    for day, job in plan.paid_days:
        by_job.setdefault(job, []).append(day)
    for job, days in by_job.items():
        _wait_for(client, job, f"the earlier purchase {job}", progress=progress, cancel=cancel, sleep=sleep,
                  poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)
        stamps = {day.replace("-", "") for day in days}
        wanted = [item for item in data_files(client.list_files(job)) if any(s in item.filename for s in stamps)]
        files += _fetch(client, wanted, folder / job, progress=progress, cancel=cancel)
    # 2. new days: buy (recording the purchase at once), wait, download
    ledger = read_ledger(folder)
    for index, (start, end) in enumerate(plan.runs, 1):
        _stop_if(cancel, before_buying=True)
        label = f"{start}" if (end - start).days == 1 else f"{start} to {end - timedelta(days=1)}"
        _emit(progress, "submitting", f"Buying {label} ({index} of {len(plan.runs)})", index - 1, len(plan.runs))
        job = client.submit_job(RangeRequest(start, end))
        for offset in range((end - start).days):
            ledger[(start + timedelta(days=offset)).isoformat()] = job
        write_ledger(folder, ledger)
        _wait_for(client, job, label, progress=progress, cancel=cancel, sleep=sleep,
                  poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)
        files += _fetch(client, data_files(client.list_files(job)), folder / job, progress=progress, cancel=cancel)
    _emit(progress, "done", f"Downloaded {len(files)} file(s); importing them now.", 1, 1)
    return files


def data_files(files: Sequence[RemoteFile]) -> list[RemoteFile]:
    """The daily data files of a job (not its metadata, condition or symbology files)."""
    return sorted((item for item in files if item.filename.endswith(DATA_SUFFIX)), key=lambda item: item.filename)


def remove_downloads(paths: Iterable[Path]) -> None:
    """Delete downloaded files once they are imported (they can be several GB)."""
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
            parent = Path(path).parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            continue


def _fetch(
    client: HistoricalClient, wanted: Sequence[RemoteFile], folder: Path, *,
    progress: ProgressSink | None, cancel: Callable[[], bool] | None,
) -> list[Path]:
    paths: list[Path] = []
    for number, remote in enumerate(wanted, 1):
        _stop_if(cancel, before_buying=False)
        _emit(progress, "downloading", f"Downloading {remote.filename} ({number} of {len(wanted)})", 0, remote.size)
        paths.append(client.download(
            remote, folder / remote.filename, cancel=cancel,
            progress=lambda received, total, name=remote.filename: _emit(
                progress, "downloading", f"Downloading {name}", received, total),
        ))
    return paths


def _wait_for(
    client: HistoricalClient, job_id: str, label: str, *, progress: ProgressSink | None,
    cancel: Callable[[], bool] | None, sleep: Callable[[float], None], poll_seconds: float, max_wait_seconds: float,
) -> None:
    waited = 0.0
    while True:
        state = client.job_state(job_id)
        if state == "done":
            return
        if state not in _LIVE_STATES:
            raise DatabentoError(
                f"Databento job {job_id} for {label} ended as {state!r}; check the price again to buy it again.")
        if waited >= max_wait_seconds:
            raise DatabentoError(
                f"Databento is still preparing {label} after {int(waited // 60)} minutes. It is already paid "
                "for: press Download again later and it continues from that purchase without paying again.")
        _stop_if(cancel, before_buying=False, job_id=job_id)
        _emit(progress, "waiting", f"Databento is preparing {label} ({state})", 0, 0)
        sleep(poll_seconds)
        waited += poll_seconds


def _stop_pricing(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise DatabentoError("Price check stopped by you. Nothing was bought.")


def _stop_if(cancel: Callable[[], bool] | None, *, before_buying: bool, job_id: str = "") -> None:
    if cancel is None or not cancel():
        return
    if before_buying:
        raise DatabentoError("Stopped by you before buying anything more.")
    tail = f" (job {job_id})" if job_id else ""
    raise DatabentoError(
        f"Stopped by you. What was already bought is kept{tail}: the next Download continues it without paying again.")


def _emit(sink: ProgressSink | None, stage: str, message: str, done: int, total: int) -> None:
    if sink is not None:
        sink(DownloadEvent(stage, message, done, total))
