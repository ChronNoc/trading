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
from datetime import time as clock
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

from app.importers.databento import imported_days
from app.importers.databento_api import (
    DatabentoError,
    DatabentoUnreachable,
    HistoricalClient,
    RangeRequest,
    RemoteFile,
)

MAX_DAYS_PER_PLAN = 366
# Regular trading hours, New York: from the opening bell (so the first hour and its levels are
# complete) to just after Lucid's 16:45 auto-close (the governor is flat by 16:40). Starting any
# earlier would compute an "overnight" high/low from a sliver of the night, a level that does not
# exist; starting at the bell leaves the overnight levels honestly unknown. Both ends sit on
# 10-minute boundaries, where Databento's price check is exact.
RTH_WINDOW = (clock(9, 30), clock(16, 50))
DATA_SUFFIX = ".csv.zst"
DOWNLOAD_FOLDER = ".databento_downloads"   # beside data/raw, never inside it
LEDGER_FILE = "jobs.json"
CENT = Decimal("0.01")
_LIVE_STATES = ("received", "queued", "processing", "done")
# A slow reply or dropped connection on a call that buys nothing is tried again after these pauses
# (seconds; about 8 minutes in all) before the run gives up. On 2026-11-09 one 60-second silence
# while a paid job was being prepared ended a whole run.
RETRY_PAUSES = (15.0, 30.0, 60.0, 120.0, 240.0)
# While a paid job is being prepared, this many unanswered status checks in a row (each up to the
# client's timeout, then the poll interval) are waited out before giving up.
SILENT_POLLS = 20
_T = TypeVar("_T")
_PAID_NOTE = ("It is already paid for: later, press Check price and then the Buy or Download button; it "
              "continues from that purchase without paying again.")


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    """What a download would fetch and cost."""

    first_day: date
    last_day: date                                   # inclusive
    runs: tuple[tuple[date, date], ...]              # (start, end exclusive): consecutive days to BUY
    costs: tuple[Decimal, ...]                       # Databento's price of each run, US dollars
    paid_days: tuple[tuple[str, str], ...]           # (day, job id): bought earlier, only to download
    already_imported: tuple[str, ...]                # days left out: already imported
    rth_only: bool = False                           # each day only RTH_WINDOW (one purchase per day)

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


def recent_trading_days(last: date, count: int) -> list[date]:
    """The last ``count`` weekdays up to and including ``last`` (exchange holidays not removed)."""
    if count < 1:
        raise ValueError("count must be at least 1")
    days: list[date] = []
    day = last
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def range_request(start: date, end: date, rth_only: bool) -> RangeRequest:
    """The Databento request for one run: whole days, or one day's regular trading hours."""
    return RangeRequest(start, end, window=RTH_WINDOW if rth_only else None)


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


def _read_document(folder: Path) -> dict[str, object]:
    try:
        document = json.loads((Path(folder) / LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def read_ledger(folder: Path) -> dict[str, str]:
    """Day (YYYY-MM-DD) -> the Databento job that bought it."""
    days = _read_document(folder).get("days")
    return {str(day): str(job) for day, job in days.items()} if isinstance(days, dict) else {}


def read_ledger_hours(folder: Path) -> dict[str, str]:
    """Day -> the hours that were bought: ``rth`` or ``full`` (older entries were whole days)."""
    hours = _read_document(folder).get("hours")
    recorded = {str(day): str(kind) for day, kind in hours.items()} if isinstance(hours, dict) else {}
    return {day: recorded.get(day, "full") for day in read_ledger(folder)}


def write_ledger(folder: Path, days: dict[str, str], hours: dict[str, str] | None = None) -> None:
    """Save the ledger atomically: the job behind each day and which hours it bought."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LEDGER_FILE
    temporary = target.with_name(target.name + ".tmp")
    kinds = {day: (hours or {}).get(day, "full") for day in days}
    document = {"version": 2, "days": dict(sorted(days.items())), "hours": dict(sorted(kinds.items()))}
    temporary.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _covers(bought: str, rth_only: bool) -> bool:
    """A whole day bought covers both requests; trading hours bought only cover trading hours."""
    return bought == "full" or rth_only


# --- plan -----------------------------------------------------------------------------------------


def plan_download(
    client: HistoricalClient, out_root: Path, first_day: date, last_day: date, *,
    now: datetime | None = None, progress: ProgressSink | None = None, cancel: Callable[[], bool] | None = None,
    rth_only: bool = False,
) -> DownloadPlan:
    """Price exactly the days of [first_day, last_day] still needed. Spends nothing.

    ``rth_only`` buys only the regular trading hours (``RTH_WINDOW``) of each weekday: weekends
    are left out and every day is its own purchase (a batch job has one start and one end).
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
    if rth_only:
        every_day = [day for day in every_day if day.weekday() < 5]
    needed = [day for day in every_day if day.isoformat() not in imported]
    ledger = read_ledger(download_folder(out_root))
    bought_hours = read_ledger_hours(download_folder(out_root))
    live_jobs: dict[str, bool] = {}
    paid: list[tuple[str, str]] = []
    to_buy: list[date] = []
    for day in needed:
        job = ledger.get(day.isoformat())
        if job is not None and not _covers(bought_hours.get(day.isoformat(), "full"), rth_only):
            job = None   # e.g. only the trading hours were bought, and now the whole day is wanted
        if job is not None and job not in live_jobs:
            _stop_pricing(cancel)
            _emit(progress, "pricing", f"Checking the earlier Databento job {job}", 0, 0)
            live_jobs[job] = client.job_state(job) in _LIVE_STATES
        if job is not None and live_jobs[job]:
            paid.append((day.isoformat(), job))
        else:
            to_buy.append(day)
    runs = [(day, day + timedelta(days=1)) for day in sorted(to_buy)] if rth_only else missing_runs(to_buy)
    costs: list[Decimal] = []
    for index, (start, end) in enumerate(runs, 1):
        _stop_pricing(cancel)
        _emit(progress, "pricing", f"Asking Databento for the price ({index} of {len(runs)})", index - 1, len(runs))
        costs.append(client.get_cost(range_request(start, end, rth_only)))
    already = tuple(day.isoformat() for day in every_day if day.isoformat() in imported)
    return DownloadPlan(first_day, last_day, tuple(runs), tuple(costs), tuple(paid), already, rth_only)


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
    patient = {"sleep": sleep, "progress": progress, "cancel": cancel}
    fresh = Decimal("0")
    for start, end in plan.runs:
        fresh += _patiently(lambda s=start, e=end: client.get_cost(range_request(s, e, plan.rth_only)),
                            "the price", before_buying=True, **patient)
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
        listing = _patiently(lambda j=job: client.list_files(j), "the file list", job_id=job, **patient)
        wanted = [item for item in data_files(listing) if any(s in item.filename for s in stamps)]
        files += _fetch(client, wanted, folder / job, **patient)
    # 2. new days: buy (recording the purchase at once), wait, download
    ledger = read_ledger(folder)
    hours = read_ledger_hours(folder)
    for index, (start, end) in enumerate(plan.runs, 1):
        _stop_if(cancel, before_buying=True)
        label = f"{start}" if (end - start).days == 1 else f"{start} to {end - timedelta(days=1)}"
        _emit(progress, "submitting", f"Buying {label} ({index} of {len(plan.runs)})", index - 1, len(plan.runs))
        request = range_request(start, end, plan.rth_only)
        asked = datetime.now(UTC) - timedelta(minutes=5)   # margin for a clock that runs fast
        try:
            job = client.submit_job(request)               # the purchase: never repeated blindly
        except DatabentoUnreachable:
            job = _lost_purchase(client, request, label, asked, sleep=sleep, progress=progress)
        for offset in range((end - start).days):
            day = (start + timedelta(days=offset)).isoformat()
            ledger[day] = job
            hours[day] = "rth" if plan.rth_only else "full"
        write_ledger(folder, ledger, hours)
        _wait_for(client, job, label, progress=progress, cancel=cancel, sleep=sleep,
                  poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)
        listing = _patiently(lambda j=job: client.list_files(j), "the file list", job_id=job, **patient)
        files += _fetch(client, data_files(listing), folder / job, **patient)
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
    progress: ProgressSink | None, cancel: Callable[[], bool] | None, sleep: Callable[[float], None],
) -> list[Path]:
    paths: list[Path] = []
    for number, remote in enumerate(wanted, 1):
        _stop_if(cancel, before_buying=False)
        _emit(progress, "downloading", f"Downloading {remote.filename} ({number} of {len(wanted)})", 0, remote.size)
        paths.append(_patiently(
            lambda remote=remote: client.download(
                remote, folder / remote.filename, cancel=cancel,
                progress=lambda received, total, name=remote.filename: _emit(
                    progress, "downloading", f"Downloading {name}", received, total),
            ),
            remote.filename, sleep=sleep, progress=progress, cancel=cancel,
        ))
    return paths


def _wait_for(
    client: HistoricalClient, job_id: str, label: str, *, progress: ProgressSink | None,
    cancel: Callable[[], bool] | None, sleep: Callable[[float], None], poll_seconds: float, max_wait_seconds: float,
) -> None:
    waited = 0.0
    silent = 0
    while True:
        try:
            state: str | None = client.job_state(job_id)
            silent = 0
        except DatabentoUnreachable:
            silent += 1             # a paid job: a silent Databento is waited out, not given up on
            if silent > SILENT_POLLS:
                raise DatabentoError(f"Databento stopped answering while preparing {label}. {_PAID_NOTE}") from None
            state = None
        if state == "done":
            return
        if state is not None and state not in _LIVE_STATES:
            raise DatabentoError(
                f"Databento job {job_id} for {label} ended as {state!r}; check the price again to buy it again.")
        if waited >= max_wait_seconds:
            raise DatabentoError(
                f"Databento is still preparing {label} after {int(waited // 60)} minutes. {_PAID_NOTE}")
        _stop_if(cancel, before_buying=False, job_id=job_id)
        _emit(progress, "waiting", f"Databento is preparing {label} ({state})" if state is not None
              else f"Databento did not answer about {label}; asking again", 0, 0)
        sleep(poll_seconds)
        waited += poll_seconds


def _patiently(
    call: Callable[[], _T], what: str, *, sleep: Callable[[float], None], progress: ProgressSink | None,
    cancel: Callable[[], bool] | None, before_buying: bool = False, job_id: str = "",
) -> _T:
    """Run a call that buys nothing, trying again while Databento is slow or the connection drops."""
    for attempt, pause in enumerate(RETRY_PAUSES, 1):
        try:
            return call()
        except DatabentoUnreachable:
            _stop_if(cancel, before_buying=before_buying, job_id=job_id)
            _emit(progress, "waiting", f"Databento did not answer for {what}; trying again in {int(pause)} "
                  f"seconds ({attempt} of {len(RETRY_PAUSES)}).", 0, 0)
            sleep(pause)
    return call()


def _lost_purchase(
    client: HistoricalClient, request: RangeRequest, label: str, since: datetime, *,
    sleep: Callable[[float], None], progress: ProgressSink | None,
) -> str:
    """The reply to a purchase was lost: ask Databento whether it went through, never buy again.

    Returns the job id when Databento lists the purchase; otherwise raises, telling the user how to
    make sure before buying that day again.
    """
    check = ("Before buying it again, look under Download center in the Databento portal: if a job for "
             f"{label} is there, it is already paid for.")
    for pause in (*RETRY_PAUSES[:2], None):         # a new job can take a moment to appear in the list
        try:
            job = _patiently(lambda: client.find_job(request, since), "the purchase", sleep=sleep,
                             progress=progress, cancel=None)
        except DatabentoError as error:
            raise DatabentoError(
                f"Databento did not confirm the purchase of {label}, and the app could not check whether it went "
                f"through ({error}). {check}") from None
        if job is not None:
            _emit(progress, "submitting", f"Databento's reply was lost, but the purchase of {label} went through "
                  f"(job {job}); continuing.", 0, 0)
            return str(job)
        if pause is not None:
            _emit(progress, "waiting", f"Checking whether the purchase of {label} went through", 0, 0)
            sleep(pause)
    raise DatabentoError(
        f"Databento did not confirm the purchase of {label}, and no job for it appears in your account, so it was "
        f"most likely not bought. {check}")


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
        f"Stopped by you. What was already bought is kept{tail}: the next Buy or Download continues it without "
        "paying again.")


def _emit(sink: ProgressSink | None, stage: str, message: str, done: int, total: int) -> None:
    if sink is not None:
        sink(DownloadEvent(stage, message, done, total))
