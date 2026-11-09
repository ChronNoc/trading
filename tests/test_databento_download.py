"""The Databento downloader: the API client, the key store, and the plan/buy/download/import flow.

Everything runs against a fake Databento server (no network, no money). The fake answers the
same HTTP calls Databento's own client makes, and records every request so the tests can prove
what would have been sent, and above all what would have been BOUGHT.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

import app.importers.databento_download as downloads
from app.importers.databento import ImportOptions, import_many, imported_days
from app.importers.databento_api import (
    DatabentoAuthError,
    DatabentoError,
    HistoricalClient,
    RangeRequest,
    RemoteFile,
    _KeySafeRedirect,
)
from app.importers.databento_download import (
    RTH_WINDOW,
    download_folder,
    download_plan,
    missing_runs,
    plan_download,
    read_ledger,
    recent_trading_days,
)
from app.importers.databento_key import (
    describe_api_key,
    forget_api_key,
    key_file,
    load_api_key,
    save_api_key,
    validate_api_key,
)
from tests.databento_fixtures import BASE_TS, DAY_NS, day_rows, mbp_header, write_compressed

KEY = "db-TestKey1234567890abcdef"
NOW = datetime(2025, 7, 20, 12, tzinfo=UTC)         # "today" for the tests; history ends 2025-07-19
PRICE_PER_DAY = D("0.42")
RTH_PRICE = D("0.17")                                # one day's trading hours


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


class DroppedResponse(FakeResponse):
    """A download whose connection is reset after the first chunk."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise ConnectionResetError("connection reset by peer")
        return super().read(max(1, len(self._stream.getvalue()) // 2))


class FakeDatabento:
    """The handful of Databento Historical calls the downloader makes, in memory."""

    def __init__(self, files_by_day: dict[str, bytes] | None = None) -> None:
        self.files_by_day = files_by_day or {}
        self.requests: list[urllib.request.Request] = []
        self.jobs: dict[str, dict[str, object]] = {}
        self.polls_until_done = 1
        self.fail_with: urllib.error.HTTPError | None = None
        self.silent: dict[str, int] = {}      # endpoint -> how many calls time out before it answers
        self.broken_downloads = 0             # downloads whose connection drops halfway
        self.submit_reply_lost = False        # the purchase goes through, but its reply never arrives
        self.submit_never_arrives = False     # the purchase request itself is lost: nothing is bought

    # the opener the client calls
    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        parts = urllib.parse.urlsplit(request.full_url)
        form = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        query = dict(urllib.parse.parse_qsl(parts.query))
        endpoint = parts.path.rsplit("/", 1)[-1]
        if self.silent.get(endpoint, 0) > 0:
            self.silent[endpoint] -= 1
            raise TimeoutError("timed out")
        if endpoint == "batch.submit_job" and self.submit_never_arrives:
            raise TimeoutError("timed out")
        if endpoint == "batch.list_jobs":
            return self.json(json.dumps([self.as_listed(job_id) for job_id in self.jobs]))
        if endpoint == "metadata.get_cost":
            if "T" in form["start"]:                   # a time window: one day's trading hours
                return self.json(str(RTH_PRICE))
            days = (date.fromisoformat(form["end"]) - date.fromisoformat(form["start"])).days
            return self.json(str(PRICE_PER_DAY * days))
        if endpoint == "batch.submit_job":
            job_id = f"GLBX-{len(self.jobs) + 1:04d}"
            self.jobs[job_id] = {"form": form, "polls": 0}
            if self.submit_reply_lost:
                raise TimeoutError("timed out")
            return self.json(json.dumps({"id": job_id, "state": "queued"}))
        if endpoint == "batch.get_job_details":
            job = self.jobs[query["job_id"]]
            if job.get("expired"):
                return self.json(json.dumps({"id": query["job_id"], "state": "expired"}))
            job["polls"] = int(job["polls"]) + 1  # type: ignore[call-overload]
            state = "done" if job["polls"] > self.polls_until_done else "processing"
            return self.json(json.dumps({"id": query["job_id"], "state": state}))
        if endpoint == "batch.list_files":
            return self.json(json.dumps(self.listing(query["job_id"])))
        if "/download/" in parts.path:
            name = parts.path.rsplit("/", 1)[-1]
            if self.broken_downloads > 0 and name != "metadata.json":
                self.broken_downloads -= 1
                return DroppedResponse(self.content(name))
            return FakeResponse(self.content(name))
        raise AssertionError(f"unexpected request {request.get_method()} {request.full_url}")

    @staticmethod
    def json(text: str) -> FakeResponse:
        return FakeResponse(text.encode())

    def as_listed(self, job_id: str) -> dict[str, object]:
        """A job as ``batch.list_jobs`` shows it (Databento writes nanosecond ISO times)."""
        form = self.jobs[job_id]["form"]

        def moment(value: str) -> str:
            return f"{value}:00.000000000Z" if "T" in value else f"{value}T00:00:00.000000000Z"

        return {"id": job_id, "dataset": form["dataset"], "schema": form["schema"],  # type: ignore[index]
                "symbols": form["symbols"], "start": moment(str(form["start"])),  # type: ignore[index]
                "end": moment(str(form["end"])), "state": "queued"}  # type: ignore[index]

    def days_of(self, job_id: str) -> list[str]:
        form = self.jobs[job_id]["form"]
        if "T" in str(form["start"]):  # type: ignore[index]
            return [str(form["start"])[:10]]  # type: ignore[index]
        start, end = date.fromisoformat(form["start"]), date.fromisoformat(form["end"])  # type: ignore[index]
        return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days)]

    def listing(self, job_id: str) -> list[dict[str, object]]:
        items = []
        for day in self.days_of(job_id):
            name = f"glbx-mdp3-{day.replace('-', '')}.mbp-10.csv.zst"
            body = self.content(name)
            items.append({"filename": name, "size": len(body), "hash": "sha256:" + hashlib.sha256(body).hexdigest(),
                          "urls": {"https": f"https://hist.databento.com/v0/batch/download/{job_id}/{name}"}})
        items.append({"filename": "metadata.json", "size": 2, "hash": "", "urls": {
            "https": f"https://hist.databento.com/v0/batch/download/{job_id}/metadata.json"}})
        return items

    def content(self, name: str) -> bytes:
        if name == "metadata.json":
            return b"{}"
        stamp = name.split("-")[2].split(".")[0]
        day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        return self.files_by_day.get(day, b"not a real file")

    def bought(self) -> list[dict[str, object]]:
        return [job["form"] for job in self.jobs.values()]  # type: ignore[misc]

    def endpoints(self) -> list[str]:
        return [urllib.parse.urlsplit(r.full_url).path.rsplit("/", 1)[-1] for r in self.requests]


def client(fake: FakeDatabento) -> HistoricalClient:
    return HistoricalClient(KEY, opener=fake)


def http_error(code: int, detail: str) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"detail": detail}).encode())
    return urllib.error.HTTPError("https://hist.databento.com/v0/x", code, "error", {}, body)  # type: ignore[arg-type]


# --- the client -----------------------------------------------------------------------------------


def test_the_price_check_is_the_exact_databento_call_with_basic_auth() -> None:
    fake = FakeDatabento()
    cost = client(fake).get_cost(RangeRequest(date(2025, 7, 15), date(2025, 7, 18)))
    assert cost == D("1.26")
    request = fake.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://hist.databento.com/v0/metadata.get_cost"
    assert dict(urllib.parse.parse_qsl(request.data.decode())) == {
        "dataset": "GLBX.MDP3", "start": "2025-07-15", "end": "2025-07-18", "symbols": "MNQ.v.0",
        "schema": "mbp-10", "stype_in": "continuous", "stype_out": "instrument_id"}
    expected = "Basic " + base64.b64encode(f"{KEY}:".encode()).decode()
    assert request.get_header("Authorization") == expected


def test_a_batch_job_asks_for_daily_zstd_csv_with_symbols() -> None:
    fake = FakeDatabento()
    job = client(fake).submit_job(RangeRequest(date(2025, 7, 15), date(2025, 7, 16)))
    form = fake.bought()[0]
    assert job == "GLBX-0001"
    assert {key: form[key] for key in ("encoding", "compression", "split_duration", "delivery", "map_symbols")} == {
        "encoding": "csv", "compression": "zstd", "split_duration": "day", "delivery": "download", "map_symbols": "True"}


def test_the_key_never_appears_in_a_repr_or_an_error_message() -> None:
    fake = FakeDatabento()
    fake.fail_with = http_error(401, "Invalid API key")
    api = client(fake)
    assert KEY not in repr(api)
    with pytest.raises(DatabentoAuthError) as raised:
        api.get_cost(RangeRequest(date(2025, 7, 15), date(2025, 7, 16)))
    assert KEY not in str(raised.value) and "did not accept the API key" in str(raised.value)


@pytest.mark.parametrize(("code", "words"), [(402, "payment or credit"), (422, "HTTP 422: bad symbol")])
def test_databento_refusals_are_explained_in_plain_words(code: int, words: str) -> None:
    fake = FakeDatabento()
    fake.fail_with = http_error(code, "bad symbol")
    with pytest.raises(DatabentoError) as raised:
        client(fake).get_cost(RangeRequest(date(2025, 7, 15), date(2025, 7, 16)))
    assert words.split(":")[0] in str(raised.value)


def test_the_key_is_only_ever_sent_to_databento_over_https() -> None:
    fake = FakeDatabento()
    api = client(fake)
    for url in ("http://hist.databento.com/x", "https://evil.example.com/x", "https://databento.com.evil.io/x"):
        with pytest.raises(DatabentoError, match="Refusing to send the Databento key"):
            api.download(RemoteFile("x.csv.zst", 1, url), Path("unused"))
    assert fake.requests == []
    with pytest.raises(ValueError):
        HistoricalClient(KEY, gateway="http://hist.databento.com")


def test_a_redirect_away_from_databento_drops_the_key() -> None:
    handler = _KeySafeRedirect()
    original = urllib.request.Request("https://hist.databento.com/v0/batch/download/J/f",
                                      headers={"Authorization": "Basic secret"})
    away = handler.redirect_request(original, None, 302, "Found", {}, "https://bucket.s3.amazonaws.com/f")
    assert away is not None and not away.has_header("Authorization")
    home = handler.redirect_request(original, None, 302, "Found", {}, "https://cdn.databento.com/f")
    assert home is not None and home.has_header("Authorization")


def test_a_download_is_checked_against_its_size_and_hash_and_resumes(tmp_path: Path) -> None:
    fake = FakeDatabento({"2025-07-15": b"day one data"})
    api = client(fake)
    body = b"day one data"
    good = RemoteFile("glbx-mdp3-20250715.mbp-10.csv.zst", len(body),
                      "https://hist.databento.com/v0/batch/download/J/glbx-mdp3-20250715.mbp-10.csv.zst",
                      hashlib.sha256(body).hexdigest())
    target = api.download(good, tmp_path / good.filename)
    assert target.read_bytes() == body
    asked = len(fake.requests)
    api.download(good, tmp_path / good.filename)                         # already complete: no new request
    assert len(fake.requests) == asked
    corrupt = RemoteFile(good.filename, len(body), good.url, "0" * 64)
    with pytest.raises(DatabentoError, match="corrupt"):
        api.download(corrupt, tmp_path / "other.csv.zst")
    assert not (tmp_path / "other.csv.zst").exists() and not list(tmp_path.glob("*.part"))


# --- planning -------------------------------------------------------------------------------------


def test_missing_days_are_grouped_into_runs() -> None:
    days = [date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 4), date(2025, 7, 5), date(2025, 7, 9)]
    assert missing_runs(days) == [(date(2025, 7, 1), date(2025, 7, 3)), (date(2025, 7, 4), date(2025, 7, 6)),
                                  (date(2025, 7, 9), date(2025, 7, 10))]


def test_a_plan_prices_only_the_days_not_imported_and_buys_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: {"2025-07-16"})
    fake = FakeDatabento()
    plan = plan_download(client(fake), tmp_path / "raw", date(2025, 7, 15), date(2025, 7, 18), now=NOW)
    assert plan.runs == ((date(2025, 7, 15), date(2025, 7, 16)), (date(2025, 7, 17), date(2025, 7, 19)))
    assert plan.total_cost == PRICE_PER_DAY * 3 and plan.days_to_buy == 3
    assert plan.already_imported == ("2025-07-16",)
    assert set(fake.endpoints()) == {"metadata.get_cost"} and fake.bought() == []


@pytest.mark.parametrize(("first", "last", "words"), [
    (date(2025, 7, 18), date(2025, 7, 15), "must not be after"),
    (date(2025, 7, 15), date(2025, 7, 20), "up to yesterday"),
    (date(2024, 1, 1), date(2025, 7, 19), "At most 366 days"),
])
def test_impossible_ranges_are_refused_before_asking_databento(tmp_path: Path, first, last, words) -> None:
    fake = FakeDatabento()
    with pytest.raises(ValueError, match=words):
        plan_download(client(fake), tmp_path / "raw", first, last, now=NOW)
    assert fake.requests == []


# --- buying: never more than confirmed, never twice ------------------------------------------------


def test_nothing_is_bought_when_the_price_rose_above_what_was_confirmed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    plan = plan_download(client(fake), tmp_path / "raw", date(2025, 7, 15), date(2025, 7, 16), now=NOW)
    with pytest.raises(DatabentoError, match="Nothing was bought"):
        download_plan(client(fake), plan, tmp_path / "raw", confirmed_cost=plan.total_cost - D("0.01"),
                      sleep=lambda s: None)
    assert fake.bought() == []


def test_stopping_before_the_purchase_buys_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    plan = plan_download(client(fake), tmp_path / "raw", date(2025, 7, 15), date(2025, 7, 15), now=NOW)
    with pytest.raises(DatabentoError, match="before buying"):
        download_plan(client(fake), plan, tmp_path / "raw", confirmed_cost=plan.total_cost,
                      cancel=lambda: True, sleep=lambda s: None)
    assert fake.bought() == []


def test_a_purchase_is_recorded_at_once_and_a_retry_never_pays_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    fake.polls_until_done = 10**6                      # Databento is slow: the first run gives up waiting
    raw = tmp_path / "raw"
    plan = plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 16), now=NOW)
    with pytest.raises(DatabentoError, match="already paid"):
        download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None,
                      poll_seconds=1, max_wait_seconds=3)
    assert len(fake.bought()) == 1
    assert read_ledger(download_folder(raw)) == {"2025-07-15": "GLBX-0001", "2025-07-16": "GLBX-0001"}

    fake.polls_until_done = 0                          # later: Databento has finished
    again = plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 16), now=NOW)
    assert again.total_cost == 0 and again.runs == () and len(again.paid_days) == 2
    files = download_plan(client(fake), again, raw, confirmed_cost=D("0"), sleep=lambda s: None)
    assert len(fake.bought()) == 1                     # still exactly one purchase
    assert [p.name for p in files] == ["glbx-mdp3-20250715.mbp-10.csv.zst", "glbx-mdp3-20250716.mbp-10.csv.zst"]


def test_an_expired_earlier_purchase_is_priced_again_not_assumed_paid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    downloads.write_ledger(download_folder(raw), {"2025-07-15": "GLBX-0001"})
    fake.jobs["GLBX-0001"] = {"form": {"start": "2025-07-15", "end": "2025-07-16"}, "polls": 0, "expired": True}
    plan = plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 15), now=NOW)
    assert plan.paid_days == () and plan.total_cost == PRICE_PER_DAY


# --- a slow, silent or dropped Databento (2026-11-09: one 60-second silence ended a paid run) --------


def one_paid_day(fake: FakeDatabento, raw: Path, monkeypatch) -> downloads.DownloadPlan:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    return plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 15), now=NOW, rth_only=True)


def test_a_silent_databento_while_a_paid_job_is_prepared_is_waited_out(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    plan = one_paid_day(fake, raw, monkeypatch)
    fake.silent = {"batch.get_job_details": 3, "batch.list_files": 2}
    events: list[str] = []
    files = download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost,
                          progress=lambda event: events.append(event.message), sleep=lambda s: None)
    assert [p.name for p in files] == ["glbx-mdp3-20250715.mbp-10.csv.zst"]
    assert len(fake.bought()) == 1
    assert any("did not answer" in message for message in events)


def test_a_dropped_download_is_fetched_again_and_checked(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDatabento({"2025-07-15": b"x" * 5000})
    raw = tmp_path / "raw"
    plan = one_paid_day(fake, raw, monkeypatch)
    fake.broken_downloads = 1
    files = download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None)
    assert files[0].read_bytes() == b"x" * 5000
    assert not list(files[0].parent.glob("*.part"))
    assert len(fake.bought()) == 1


def test_a_databento_that_never_answers_gives_up_with_the_purchase_kept(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    plan = one_paid_day(fake, raw, monkeypatch)
    fake.silent = {"batch.get_job_details": 10**6}
    with pytest.raises(DatabentoError, match="already paid for"):
        download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None)
    assert read_ledger(download_folder(raw)) == {"2025-07-15": "GLBX-0001"}
    assert len(fake.bought()) == 1


def test_a_lost_purchase_reply_is_found_in_the_account_not_bought_again(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    plan = one_paid_day(fake, raw, monkeypatch)
    fake.submit_reply_lost = True                      # bought, but the reply never arrived
    files = download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None)
    assert fake.endpoints().count("batch.submit_job") == 1
    assert len(fake.bought()) == 1 and len(files) == 1
    assert read_ledger(download_folder(raw)) == {"2025-07-15": "GLBX-0001"}


def test_a_lost_purchase_request_stops_without_buying_and_says_how_to_be_sure(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    plan = one_paid_day(fake, raw, monkeypatch)
    fake.submit_never_arrives = True
    with pytest.raises(DatabentoError, match="most likely not bought") as raised:
        download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None)
    assert "Download center" in str(raised.value)
    assert fake.endpoints().count("batch.submit_job") == 1   # a purchase is never repeated blindly
    assert fake.bought() == [] and read_ledger(download_folder(raw)) == {}


@pytest.mark.parametrize(("listed", "wanted", "same"), [
    ("2025-07-15T13:30:00.000000000Z", "2025-07-15T13:30", True),
    ("2025-07-15 13:30:00+00:00", "2025-07-15T13:30", True),
    ("2025-07-15T13:31:00.000000000Z", "2025-07-15T13:30", False),
    ("2025-07-15T13:30:00-05:00", "2025-07-15T13:30", False),
    ("2025-07-15T00:00:00.000000000Z", "2025-07-15", True),
    ("2025-07-15T13:30:00.000000000Z", "2025-07-15", False),
    (1752586200000000000, "2025-07-15T13:30", False),
])
def test_a_listed_job_matches_only_the_exact_moment(listed, wanted: str, same: bool) -> None:
    from app.importers.databento_api import _same_moment

    assert _same_moment(listed, wanted) is same


@pytest.mark.parametrize("code", [429, 500, 503])
def test_a_busy_databento_is_told_apart_from_a_refusal(code: int) -> None:
    from app.importers.databento_api import DatabentoUnreachable

    fake = FakeDatabento()
    fake.fail_with = http_error(code, "busy")
    with pytest.raises(DatabentoUnreachable, match="busy"):
        client(fake).get_cost(RangeRequest(date(2025, 7, 15), date(2025, 7, 16)))


# --- end to end: buy, download, import, and the next plan has nothing left ---------------------------


def daily_files(tmp_path: Path, days: int = 3) -> dict[str, bytes]:
    rows = day_rows(days, rows_per_day=300)
    column = mbp_header().index("ts_recv")
    out: dict[str, bytes] = {}
    for offset in range(days):
        day = datetime.fromtimestamp((BASE_TS + offset * DAY_NS) // 10**9, tz=UTC).date().isoformat()
        chunk = [row for row in rows if (int(row[column]) - BASE_TS) // DAY_NS == offset]
        path = write_compressed(tmp_path / f"src-{day}.csv.zst", chunk, "zstd")
        out[day] = path.read_bytes()
    return out


def test_buy_download_and_import_three_days_end_to_end(tmp_path: Path) -> None:
    fake = FakeDatabento(daily_files(tmp_path))
    raw = tmp_path / "raw"
    plan = plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 17), now=NOW)
    assert plan.total_cost == PRICE_PER_DAY * 3 and plan.runs == ((date(2025, 7, 15), date(2025, 7, 18)),)
    events: list[str] = []
    files = download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost,
                          progress=lambda event: events.append(event.stage), sleep=lambda s: None)
    assert len(files) == 3 and all(path.parent.parent == download_folder(raw) for path in files)
    assert "metadata.json" not in [p.name for p in files]
    results = import_many(files, ImportOptions(out_root=raw))
    assert [r.status for r in results] == ["imported"] * 3
    assert imported_days(raw) == {"2025-07-15", "2025-07-16", "2025-07-17"}
    assert {"submitting", "waiting", "downloading", "done"} <= set(events)
    later = plan_download(client(fake), raw, date(2025, 7, 15), date(2025, 7, 17), now=NOW)
    assert later.nothing_to_do and later.total_cost == 0
    assert len(fake.bought()) == 1


# --- the key store --------------------------------------------------------------------------------


def test_the_key_file_lives_in_app_data_never_in_the_repository(tmp_path: Path) -> None:
    path = key_file({"APPDATA": str(tmp_path)})
    assert path == tmp_path / "MNQ Intelligence" / "databento_api_key.txt"
    repo = Path(__file__).resolve().parent.parent
    assert repo not in key_file().resolve().parents


def test_save_load_forget_and_the_environment_wins(tmp_path: Path) -> None:
    path = tmp_path / "k.txt"
    save_api_key("  " + KEY + " ", path)
    assert load_api_key({}, path) == KEY
    assert load_api_key({"DATABENTO_API_KEY": "db-FromEnvironment0001"}, path) == "db-FromEnvironment0001"
    assert describe_api_key(KEY) == "db-...cdef" and describe_api_key(None) == "not set"
    assert forget_api_key(path) and load_api_key({}, path) is None and not forget_api_key(path)


@pytest.mark.parametrize("bad", ["", "   ", "abc123", "db- has space", "db-x"])
def test_a_key_that_cannot_be_a_databento_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_api_key(bad)


# --- trading hours only ------------------------------------------------------------------------------


def test_trading_hours_are_sent_as_utc_on_both_sides_of_daylight_saving() -> None:
    summer = RangeRequest(date(2026, 7, 15), date(2026, 7, 16), window=RTH_WINDOW).fields()
    winter = RangeRequest(date(2026, 11, 9), date(2026, 11, 10), window=RTH_WINDOW).fields()
    assert (summer["start"], summer["end"]) == ("2026-07-15T13:30", "2026-07-15T20:50")   # EDT, UTC-4
    assert (winter["start"], winter["end"]) == ("2026-11-09T14:30", "2026-11-09T21:50")   # EST, UTC-5
    with pytest.raises(ValueError, match="one day"):
        RangeRequest(date(2026, 7, 15), date(2026, 7, 17), window=RTH_WINDOW)


def test_trading_days_count_back_over_weekends() -> None:
    assert recent_trading_days(date(2025, 7, 21), 3) == [date(2025, 7, 17), date(2025, 7, 18), date(2025, 7, 21)]
    assert recent_trading_days(date(2025, 7, 20), 1) == [date(2025, 7, 18)]            # Sunday -> Friday


def test_a_trading_hours_plan_skips_weekends_and_buys_each_day_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    plan = plan_download(client(fake), raw, date(2025, 7, 16), date(2025, 7, 19), now=NOW, rth_only=True)
    assert plan.rth_only and plan.runs == ((date(2025, 7, 16), date(2025, 7, 17)), (date(2025, 7, 17), date(2025, 7, 18)),
                                           (date(2025, 7, 18), date(2025, 7, 19)))   # Saturday 19th left out
    assert plan.total_cost == RTH_PRICE * 3
    download_plan(client(fake), plan, raw, confirmed_cost=plan.total_cost, sleep=lambda s: None)
    starts = sorted(str(form["start"]) for form in fake.bought())
    assert starts == ["2025-07-16T13:30", "2025-07-17T13:30", "2025-07-18T13:30"]
    assert read_ledger(download_folder(raw)) == {"2025-07-16": "GLBX-0001", "2025-07-17": "GLBX-0002",
                                                 "2025-07-18": "GLBX-0003"}


def test_trading_hours_bought_earlier_never_count_as_a_whole_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    downloads.write_ledger(download_folder(raw), {"2025-07-16": "GLBX-0001"}, {"2025-07-16": "rth"})
    fake.jobs["GLBX-0001"] = {"form": {"start": "2025-07-16T13:30", "end": "2025-07-16T20:50"}, "polls": 0}
    whole = plan_download(client(fake), raw, date(2025, 7, 16), date(2025, 7, 16), now=NOW)
    assert whole.paid_days == () and whole.total_cost == PRICE_PER_DAY          # the whole day must be bought
    rth = plan_download(client(fake), raw, date(2025, 7, 16), date(2025, 7, 16), now=NOW, rth_only=True)
    assert rth.paid_days == (("2025-07-16", "GLBX-0001"),) and rth.total_cost == 0


def test_a_whole_day_bought_earlier_covers_a_trading_hours_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads, "imported_days", lambda root: set())
    fake = FakeDatabento()
    raw = tmp_path / "raw"
    downloads.write_ledger(download_folder(raw), {"2025-07-16": "GLBX-0001"})   # an older ledger: whole days
    fake.jobs["GLBX-0001"] = {"form": {"start": "2025-07-16", "end": "2025-07-17"}, "polls": 0}
    assert downloads.read_ledger_hours(download_folder(raw)) == {"2025-07-16": "full"}
    rth = plan_download(client(fake), raw, date(2025, 7, 16), date(2025, 7, 16), now=NOW, rth_only=True)
    assert rth.paid_days == (("2025-07-16", "GLBX-0001"),) and rth.total_cost == 0
