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
    download_folder,
    download_plan,
    missing_runs,
    plan_download,
    read_ledger,
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


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


class FakeDatabento:
    """The handful of Databento Historical calls the downloader makes, in memory."""

    def __init__(self, files_by_day: dict[str, bytes] | None = None) -> None:
        self.files_by_day = files_by_day or {}
        self.requests: list[urllib.request.Request] = []
        self.jobs: dict[str, dict[str, object]] = {}
        self.polls_until_done = 1
        self.fail_with: urllib.error.HTTPError | None = None

    # the opener the client calls
    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        parts = urllib.parse.urlsplit(request.full_url)
        form = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        query = dict(urllib.parse.parse_qsl(parts.query))
        endpoint = parts.path.rsplit("/", 1)[-1]
        if endpoint == "metadata.get_cost":
            days = (date.fromisoformat(form["end"]) - date.fromisoformat(form["start"])).days
            return self.json(str(PRICE_PER_DAY * days))
        if endpoint == "batch.submit_job":
            job_id = f"GLBX-{len(self.jobs) + 1:04d}"
            self.jobs[job_id] = {"form": form, "polls": 0}
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
            return FakeResponse(self.content(name))
        raise AssertionError(f"unexpected request {request.get_method()} {request.full_url}")

    @staticmethod
    def json(text: str) -> FakeResponse:
        return FakeResponse(text.encode())

    def days_of(self, job_id: str) -> list[str]:
        form = self.jobs[job_id]["form"]
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
