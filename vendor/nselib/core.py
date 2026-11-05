"""
NSESession — manages the HTTP session with NSE India.

NSE uses Akamai bot protection. The trick is:
  1. Hit the homepage first to get a valid session cookie.
  2. Use that cookie for all /api/* calls with browser-like headers.
  3. Refresh the cookie before it expires (~5 min TTL in practice).
  4. Rate-limit to 3 rps to avoid triggering NSE's throttle.
"""

import json
import time
import threading
import zipfile
import gzip
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import NSESessionError, NSEFileUnavailableError

# Module-level shared session — avoids re-seeding Akamai cookies on every with NSE() block
_shared_session: "NSESession | None" = None
_shared_folder:  str = ""

def get_shared_session(download_folder: str | Path = ".", rate: float = 3.0, timeout: int = 15) -> "NSESession":
    """Return a module-level NSESession, creating it once and reusing it across calls.

    Saves ~0.5s per call vs opening a fresh NSE() context each time (cookie re-seeding overhead).
    Thread-safe: the token bucket inside NSESession is already mutex-protected.
    """
    global _shared_session, _shared_folder
    folder = str(Path(download_folder).expanduser().resolve())
    if _shared_session is None or not _shared_session._s or _shared_folder != folder:
        if _shared_session is not None:
            try:
                _shared_session.close()
            except Exception:
                pass
        _shared_session = NSESession(download_folder=download_folder, rate=rate, timeout=timeout)
        _shared_folder  = folder
    return _shared_session


NSE_BASE    = "https://www.nseindia.com"
NSE_BASE_IN = "https://www.nseindia.in"   # mirror domain; used as fallback
NSE_API     = f"{NSE_BASE}/api"
NSE_API_IN  = f"{NSE_BASE_IN}/api"
NSE_ARCHIVE = "https://nsearchives.nseindia.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Browser-like headers that satisfy Akamai's bot check
_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
    "Referer":         f"{NSE_BASE}/",
    "Origin":          NSE_BASE,
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}

# Same headers but pointing at nseindia.in
_HEADERS_IN = {**_HEADERS, "Referer": f"{NSE_BASE_IN}/", "Origin": NSE_BASE_IN}

_HOME_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-User":  "?1",
    "Upgrade-Insecure-Requests": "1",
}


class _TokenBucket:
    """Thread-safe token bucket for rate limiting (single-process only)."""

    def __init__(self, rate: float):
        self._rate   = rate
        self._tokens = rate
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def consume(self) -> None:
        with self._lock:
            now     = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last  = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                time.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class _FileTokenBucket:
    """Cross-process token bucket using a file lock + JSON state.

    Shares the rate limit across all processes on the same machine, so two
    concurrent nselib processes (morning brief + interactive query) together
    respect the per-IP limit instead of each running at full rate.

    Falls back to _TokenBucket on platforms without fcntl (Windows).
    State file: ~/.nselib/rate_limit.json
    """

    _STATE_FILE = Path.home() / ".nselib" / "rate_limit.json"

    def __init__(self, rate: float):
        self._rate      = rate
        self._lock      = threading.Lock()   # intra-process guard
        self._fallback  = _TokenBucket(rate) # used if fcntl unavailable
        try:
            import fcntl as _fcntl  # noqa: F401
            self._has_fcntl = True
            self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        except ImportError:
            self._has_fcntl = False

    def consume(self) -> None:
        if not self._has_fcntl:
            self._fallback.consume()
            return
        with self._lock:
            self._file_consume()

    def _file_consume(self) -> None:
        import fcntl

        retries = 0
        while True:
            with open(self._STATE_FILE, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    raw = fh.read().strip()
                    state = json.loads(raw) if raw else {"tokens": self._rate, "ts": time.time()}

                    now     = time.time()
                    elapsed = now - state["ts"]
                    tokens  = min(self._rate, state["tokens"] + elapsed * self._rate)

                    if tokens >= 1:
                        # Token available — consume and write
                        state = {"tokens": tokens - 1, "ts": now}
                        fh.seek(0); fh.truncate()
                        fh.write(json.dumps(state)); fh.flush()
                        return
                    else:
                        # No token yet — record current state, release lock, sleep
                        wait = (1 - tokens) / self._rate
                        state = {"tokens": tokens, "ts": now}
                        fh.seek(0); fh.truncate()
                        fh.write(json.dumps(state)); fh.flush()
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

            # Sleep outside the lock so other processes can also run
            time.sleep(wait)
            retries += 1
            if retries > 20:   # safety: never spin forever
                return


class NSESession:
    """
    Manages HTTP session lifecycle with NSE India.

    Usage:
        session = NSESession("/tmp/nse_data")
        data = session.get("marketStatus")
        session.close()

        # or as context manager:
        with NSESession("/tmp/nse_data") as s:
            data = s.get("equity-stockIndices", params={"index": "NIFTY 50"})
    """

    _COOKIE_FILE = "nse_cookies.json"
    _COOKIE_TTL  = 270  # seconds; refresh before NSE's ~5min expiry

    def __init__(
        self,
        download_folder: str | Path,
        rate:        float = 3.0,
        timeout:     int   = 15,
        max_retries: int   = 5,
    ):
        self.download_folder = Path(download_folder)
        self.download_folder.mkdir(parents=True, exist_ok=True)

        self._timeout    = timeout
        self._bucket     = _FileTokenBucket(rate)
        self._cookie_file = self.download_folder / self._COOKIE_FILE
        self._cookie_ts:    float = 0.0
        self._cookie_ts_in: float = 0.0   # tracks nseindia.in session freshness

        # requests.Session with automatic retry on transient errors
        self._s = requests.Session()
        self._s.headers.update(_HEADERS)

        retry = Retry(
            total            = max_retries,
            backoff_factor   = 1,
            status_forcelist = {429, 502, 503, 504},
            allowed_methods  = {"GET"},
            raise_on_status  = False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._s.mount("https://", adapter)
        self._s.mount("http://",  adapter)

        self._load_cookies()

    # ------------------------------------------------------------------ #
    #  Cookie management                                                   #
    # ------------------------------------------------------------------ #

    def _load_cookies(self) -> None:
        if self._cookie_file.exists():
            try:
                data = json.loads(self._cookie_file.read_text())
                self._cookie_ts = data.get("ts", 0.0)
                self._s.cookies.update(data.get("cookies", {}))
            except Exception:
                pass

    def _save_cookies(self) -> None:
        try:
            self._cookie_file.write_text(json.dumps({
                "ts":      self._cookie_ts,
                "cookies": dict(self._s.cookies),
            }))
        except Exception:
            pass

    def _refresh_cookies(self) -> None:
        """Visit NSE homepage to get a fresh Akamai session cookie."""
        try:
            req = requests.Request("GET", NSE_BASE, headers=_HOME_HEADERS)
            prepared = self._s.prepare_request(req)
            prepared.headers = requests.structures.CaseInsensitiveDict(_HOME_HEADERS)
            resp = self._s.send(prepared, timeout=self._timeout)
            if resp.status_code == 200:
                # Only update cookies on success — a 403 from Akamai
                # (cloud IP block) must not wipe out existing valid cookies.
                self._s.cookies.update(resp.cookies)
                self._cookie_ts = time.monotonic()
                self._save_cookies()
            else:
                # Homepage blocked (cloud IP). Keep existing cookies alive by
                # bumping the timestamp so we don't retry every single call.
                self._cookie_ts = time.monotonic()
        except requests.RequestException as e:
            raise NSESessionError(
                f"Could not reach NSE homepage: {e}\n"
                "Check your network connection."
            ) from e

    def _refresh_cookies_in(self) -> None:
        """Visit nseindia.in homepage to build an Akamai session for the .in domain."""
        try:
            req      = requests.Request("GET", NSE_BASE_IN, headers=_HOME_HEADERS)
            prepared = self._s.prepare_request(req)
            prepared.headers = requests.structures.CaseInsensitiveDict(_HOME_HEADERS)
            resp = self._s.send(prepared, timeout=self._timeout)
            # Navigate one more page so Akamai sets the full cookie set (nsit, bm_sv, etc.)
            if resp.status_code == 200:
                req2      = requests.Request("GET", f"{NSE_BASE_IN}/market-data/live-equity-market", headers=_HOME_HEADERS)
                prepared2 = self._s.prepare_request(req2)
                prepared2.headers = requests.structures.CaseInsensitiveDict(_HOME_HEADERS)
                self._s.send(prepared2, timeout=self._timeout)
        except requests.RequestException:
            pass  # .in is a best-effort fallback; never hard-fail here
        finally:
            self._cookie_ts_in = time.monotonic()

    def _ensure_session(self) -> None:
        if time.monotonic() - self._cookie_ts > self._COOKIE_TTL:
            self._refresh_cookies()

    def _ensure_session_in(self) -> None:
        if time.monotonic() - self._cookie_ts_in > self._COOKIE_TTL:
            self._refresh_cookies_in()

    # ------------------------------------------------------------------ #
    #  Request helpers                                                     #
    # ------------------------------------------------------------------ #

    def get(self, path: str, params: dict | None = None) -> Any:
        """
        Rate-limited GET against the NSE /api/ endpoint.

        Flow:
          1. Try www.nseindia.com/api/<path>
          2. On 401/403, refresh cookie and retry once on .com
          3. If still 403 (cloud/IP block), try www.nseindia.in/api/<path>
          4. If .in also fails or returns empty, raise the original error
        """
        self._bucket.consume()
        self._ensure_session()

        url  = f"{NSE_API}/{path.lstrip('/')}"
        resp = self._s.get(url, params=params, timeout=self._timeout)

        if resp.status_code in (401, 403):
            # Cookie may have expired — force refresh and retry on .com
            self._cookie_ts = 0.0
            self._ensure_session()
            self._bucket.consume()
            resp = self._s.get(url, params=params, timeout=self._timeout)

        # If .com is still blocked, try nseindia.in as fallback
        if resp.status_code == 403:
            self._ensure_session_in()
            url_in = f"{NSE_API_IN}/{path.lstrip('/')}"
            try:
                self._bucket.consume()
                resp_in = self._s.get(
                    url_in, params=params, timeout=self._timeout,
                    headers=_HEADERS_IN,
                )
                if resp_in.status_code == 200:
                    data = resp_in.json()
                    if data:  # non-empty — .in has the data
                        return data
            except Exception:
                pass
            # .in didn't help — raise the original 403
            resp.raise_for_status()

        if resp.status_code == 404:
            raise NSEFileUnavailableError(url)

        resp.raise_for_status()
        return resp.json()

    def get_raw(self, url: str, stream: bool = False) -> requests.Response:
        """
        Rate-limited GET for archive downloads (bhavcopy, reports, etc.).
        Returns the raw Response so callers can stream or read bytes.
        """
        self._bucket.consume()
        resp = self._s.get(url, timeout=self._timeout, stream=stream)
        if resp.status_code == 404:
            raise NSEFileUnavailableError(url)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------ #
    #  File download helpers                                               #
    # ------------------------------------------------------------------ #

    def download_file(
        self,
        url:    str,
        folder: Path | None = None,
        filename: str | None = None,
    ) -> Path:
        """Download a file to disk. Returns the local Path."""
        dest_dir = folder or self.download_folder
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        fname = filename or url.split("/")[-1].split("?")[0]
        dest  = dest_dir / fname

        resp = self.get_raw(url, stream=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest

    def download_zip(
        self,
        url:    str,
        folder: Path | None = None,
        member: str | None  = None,
    ) -> Path:
        """
        Download a ZIP archive, extract one file (or the first file),
        delete the archive, and return the path to the extracted file.
        """
        dest_dir = folder or self.download_folder
        zip_path = self.download_file(url, folder=dest_dir)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            target = member if member and member in names else names[0]
            zf.extract(target, path=dest_dir)

        zip_path.unlink()
        return dest_dir / target

    def download_gz(
        self,
        url:    str,
        folder: Path | None = None,
        out_name: str | None = None,
    ) -> Path:
        """Download a .gz file, decompress it, delete the archive."""
        dest_dir = folder or self.download_folder
        gz_path  = self.download_file(url, folder=dest_dir)

        out = dest_dir / (out_name or gz_path.stem)
        with gzip.open(gz_path, "rb") as f_in, open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        gz_path.unlink()
        return out

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._save_cookies()
        self._s.close()

    def __enter__(self) -> "NSESession":
        return self

    def __exit__(self, *_) -> None:
        self.close()
