"""A small client for Databento's Historical API: price checks and batch downloads only.

This is the ONE module in the project allowed to use the network (the user asked for Databento
as the data source on 2026-11-09; see AGENTS.md). It never trades and never touches a broker:
it buys historical market data, after the user has seen and confirmed the price, and downloads
it as files for ``app.importers.databento`` to import.

It uses only the standard library (no SDK) and mirrors the HTTP calls of Databento's own
open-source Python client (``databento/databento-python``):

* ``POST /v0/metadata.get_cost``  - the price of a request, in US dollars;
* ``POST /v0/batch.submit_job``   - a batch job (CSV, zstd, one file per day);
* ``GET  /v0/batch.get_job_details`` and ``GET /v0/batch.list_files`` - its progress and files;
* the files themselves, from the HTTPS links Databento returns.

Authentication is HTTP basic with the API key as the user name and an empty password. The key is
sent only to ``*.databento.com`` over HTTPS and never appears in a message, log line or repr.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

GATEWAY = "https://hist.databento.com"
API_VERSION = "0"
DATASET = "GLBX.MDP3"            # CME Globex
SCHEMA = "mbp-10"                # ten book levels plus every trade
CONTINUOUS_SYMBOL = "MNQ.v.0"    # the most-traded MNQ contract each day (one instrument a day)
USER_AGENT = "mnq-intelligence-downloader/1"
_TRUSTED_HOST_SUFFIX = ".databento.com"
_CHUNK = 1 << 20


class DatabentoError(RuntimeError):
    """A request Databento refused or that could not be completed, in plain words."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DatabentoAuthError(DatabentoError):
    """Databento did not accept the API key."""


class _Response(Protocol):
    status: int

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


Opener = Callable[[urllib.request.Request, float], _Response]


def _is_trusted_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "databento.com" or host.endswith(_TRUSTED_HOST_SUFFIX)


class _KeySafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the API key to a host outside databento.com.

    The standard handler copies every header, Authorization included, to the new address; a
    download link that redirects to cloud storage would otherwise receive the key.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]  # noqa: ANN001,ANN201
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _is_trusted_host(newurl):
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_KeySafeRedirect)


def _default_opener(request: urllib.request.Request, timeout: float) -> _Response:
    return _OPENER.open(request, timeout=timeout)  # https to databento.com only, checked before every call


@dataclass(frozen=True, slots=True)
class RangeRequest:
    """One continuous range of whole UTC days of MNQ order-book data."""

    start: date                 # first UTC day, inclusive
    end: date                   # UTC day after the last one (exclusive)
    symbol: str = CONTINUOUS_SYMBOL
    dataset: str = DATASET
    schema: str = SCHEMA
    stype_in: str = "continuous"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("the end day must be after the start day")

    @property
    def days(self) -> int:
        """How many UTC days the range covers."""
        return (self.end - self.start).days

    def fields(self) -> dict[str, str]:
        """The request fields Databento's price check and batch job share."""
        return {
            "dataset": self.dataset,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": self.symbol,
            "schema": self.schema,
            "stype_in": self.stype_in,
            "stype_out": "instrument_id",
        }


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One file of a finished batch job."""

    filename: str
    size: int
    url: str
    sha256: str | None = None


@dataclass(slots=True)
class HistoricalClient:
    """Databento Historical API, over HTTPS with the standard library."""

    api_key: str = field(repr=False)
    gateway: str = GATEWAY
    timeout: float = 60.0
    opener: Opener = field(default=_default_opener, repr=False)

    def __post_init__(self) -> None:
        key = (self.api_key or "").strip()
        if not key or any(character.isspace() for character in key):
            raise DatabentoAuthError("No usable Databento API key was given.")
        self.api_key = key
        if not self.gateway.startswith("https://"):
            raise ValueError("the Databento gateway must be an https:// address")

    # -- the calls ------------------------------------------------------------------------------

    def get_cost(self, request: RangeRequest) -> Decimal:
        """What Databento would charge for ``request``, in US dollars."""
        value = self._json("POST", "metadata.get_cost", form=request.fields())
        try:
            cost = Decimal(str(value))
        except ArithmeticError as error:
            raise DatabentoError(f"Databento returned a price that is not a number: {value!r}") from error
        if not cost.is_finite() or cost < 0:
            raise DatabentoError(f"Databento returned an impossible price: {value!r}")
        return cost

    def submit_job(self, request: RangeRequest) -> str:
        """Start a batch job for ``request`` (this is the step Databento charges for). Returns its id."""
        form = dict(request.fields())
        form.update({
            "encoding": "csv",
            "compression": "zstd",
            "pretty_px": "False",
            "pretty_ts": "False",
            "map_symbols": "True",
            "split_symbols": "False",
            "split_duration": "day",
            "delivery": "download",
        })
        job = self._json("POST", "batch.submit_job", form=form)
        job_id = job.get("id") if isinstance(job, Mapping) else None
        if not job_id:
            raise DatabentoError("Databento accepted the job but did not return its id.")
        return str(job_id)

    def job_state(self, job_id: str) -> str:
        """``queued``, ``processing``, ``done`` or ``expired``."""
        job = self._json("GET", "batch.get_job_details", query={"job_id": job_id})
        state = job.get("state") if isinstance(job, Mapping) else None
        if not state:
            raise DatabentoError(f"Databento returned no state for job {job_id}.")
        return str(state)

    def list_files(self, job_id: str) -> list[RemoteFile]:
        """The files of a finished job."""
        listing = self._json("GET", "batch.list_files", query={"job_id": job_id})
        if not isinstance(listing, list):
            raise DatabentoError(f"Databento returned an unexpected file list for job {job_id}.")
        files: list[RemoteFile] = []
        for item in listing:
            urls = item.get("urls") or {}
            url = str(urls.get("https") or "")
            digest = str(item.get("hash") or "")
            files.append(RemoteFile(
                filename=str(item.get("filename") or ""),
                size=int(item.get("size") or 0),
                url=url,
                sha256=digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None,
            ))
        return files

    def download(
        self, remote: RemoteFile, destination: Path, *,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Stream ``remote`` to ``destination``, checking its size and hash; atomic on success."""
        self._require_trusted(remote.url)
        destination = Path(destination)
        if destination.is_file() and destination.stat().st_size == remote.size and self._digest_ok(destination, remote):
            return destination  # finished earlier (a resumed run): nothing to pay or fetch again
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        response = self._open(urllib.request.Request(remote.url, headers=self._headers(), method="GET"))
        received = 0
        digest = hashlib.sha256()
        try:
            with partial.open("wb") as stream:
                while True:
                    if cancel is not None and cancel():
                        raise DatabentoError("Stopped by you; the partial file was removed.")
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, remote.size)
        except BaseException:
            response.close()
            partial.unlink(missing_ok=True)
            raise
        response.close()
        if remote.size and received != remote.size:
            partial.unlink(missing_ok=True)
            raise DatabentoError(f"{remote.filename}: received {received:,} of {remote.size:,} bytes; try again.")
        if remote.sha256 and digest.hexdigest() != remote.sha256:
            partial.unlink(missing_ok=True)
            raise DatabentoError(f"{remote.filename}: the download is corrupt (checksum mismatch); try again.")
        os.replace(partial, destination)
        return destination

    # -- plumbing -------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.api_key}:".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json", "User-Agent": USER_AGENT}

    def _json(
        self, method: str, endpoint: str, *,
        form: Mapping[str, str] | None = None, query: Mapping[str, str] | None = None,
    ) -> object:
        url = f"{self.gateway}/v{API_VERSION}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        data = urllib.parse.urlencode(dict(form)).encode("ascii") if form is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = self._open(urllib.request.Request(url, data=data, headers=headers, method=method))
        try:
            body = response.read()
        finally:
            response.close()
        try:
            return json.loads(body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError) as error:
            raise DatabentoError(f"Databento sent a reply that is not JSON for {endpoint}.") from error

    def _open(self, request: urllib.request.Request) -> _Response:
        self._require_trusted(request.full_url)
        try:
            return self.opener(request, self.timeout)
        except urllib.error.HTTPError as error:
            raise _translate(error) from None
        except urllib.error.URLError as error:
            raise DatabentoError(f"Could not reach Databento ({error.reason}). Check the internet connection.") from None
        except TimeoutError:
            raise DatabentoError("Databento did not answer in time; try again.") from None

    @staticmethod
    def _require_trusted(url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not _is_trusted_host(url):
            raise DatabentoError(
                f"Refusing to send the Databento key to {parts.scheme}://{(parts.hostname or '').lower()}.")

    @staticmethod
    def _digest_ok(path: Path, remote: RemoteFile) -> bool:
        if not remote.sha256:
            return True
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest() == remote.sha256


def _translate(error: urllib.error.HTTPError) -> DatabentoError:
    """Databento's error reply, in plain words (never echoing the request or the key)."""
    detail = ""
    try:
        payload = json.loads(error.read().decode("utf-8") or "{}")
        raw = payload.get("detail") if isinstance(payload, dict) else payload
        detail = raw if isinstance(raw, str) else json.dumps(raw)[:300]
    except (ValueError, UnicodeDecodeError, OSError):
        detail = ""
    finally:
        error.close()
    suffix = f": {detail}" if detail else ""
    if error.code in (401, 403):
        return DatabentoAuthError(f"Databento did not accept the API key (HTTP {error.code}){suffix}", status=error.code)
    if error.code == 402:
        return DatabentoError(f"Databento refused the purchase (HTTP 402: payment or credit){suffix}", status=402)
    return DatabentoError(f"Databento refused the request (HTTP {error.code}){suffix}", status=error.code)
