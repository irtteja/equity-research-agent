"""
Guardian — self-healing, self-adapting, self-discovering layer.

Three jobs:
  heal     — when a request breaks, Claude diagnoses and patches it automatically
  adapt    — tracks response structure; alerts when NSE changes field names/nesting
  discover — sniffs NSE's JS bundle for API paths not yet in nselib,
             asks Claude to generate the methods, and can write them to disk
"""

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_CATALOGUE_FILE = Path(__file__).parent / "endpoints.json"


def _load_catalogue() -> dict:
    """Return the endpoint catalogue, or empty dict if unavailable."""
    try:
        if _CATALOGUE_FILE.exists():
            return json.loads(_CATALOGUE_FILE.read_text()).get("endpoints", {})
    except Exception:
        pass
    return {}

from .brain import Brain
from .core  import NSESession
from .exceptions import NSEFileUnavailableError


# Every path nselib currently uses (kept up to date)
_KNOWN_PATHS: set[str] = {
    # quotes
    "NextApi/apiClient/GetQuoteApi",
    "top-corp-info",
    # indices
    "allIndices",
    "historicalOR/indicesHistory",
    # market
    "marketStatus", "holiday-master", "etf",
    "live-analysis-volume-gainers", "market-data-pre-open",
    "block-deal", "historicalOR/bulk-block-short-deals",
    # history
    "historicalOR/cm/equity", "historicalOR/fo/derivatives",
    # corporate
    "corporates-corporateActions",
    "corporate-announcements",
    "corporate-board-meetings",
    "corporates-financial-results",
    "annual-reports", "corporates-pit", "event-calendar",
    # fno
    "NextApi/apiClient/GetQuoteApi",
    # downloads / misc
    "daily-reports",
}

_MODULE_FOR = {
    "quote":      "quotes",
    "equity":     "quotes",
    "search":     "quotes",
    "indices":    "indices",
    "historical": "history",
    "market":     "market",
    "holiday":    "market",
    "block":      "market",
    "bulk":       "market",
    "pre-open":   "market",
    "corporate":  "corporate",
    "annual":     "corporate",
    "option":     "fno",
    "derivative": "fno",
    "report":     "downloads",
}


class Guardian:
    """
    Sits between nselib and NSE's servers.

    Usage
    -----
    Automatic (recommended) — pass ``guard=True`` to NSE()::

        with NSE(anthropic_api_key="sk-ant-...", guard=True) as nse:
            nse.equity_quote("RELIANCE")   # heals itself if something breaks

    Manual::

        g = Guardian(nse._session, nse._brain)
        data = g.guarded_get("quote-equity", {"symbol": "RELIANCE"})

    Discovery::

        new = g.discover()          # finds new endpoints on NSE
        g.apply("/path/to/nselib")  # writes generated methods to module files
        g.watch(interval=3600)      # run discover() in background every hour
    """

    def __init__(self, session: NSESession, brain: Brain,
                 patches_file: str | Path | None = None,
                 log_file: str | Path | None = None):
        self._session     = session
        self._brain       = brain
        self._raw_get     = session.get   # original get, captured before any monkey-patch
        self._lock        = threading.Lock()
        self._patches:       dict[str, dict]          = {}
        self._schema_remaps: dict[str, dict[str, str]] = {}
        self._schema:        dict[str, set]            = {}
        self._heal_log:      list[dict]                = []
        self._discoveries:   list[dict]                = []
        self._patches_file: Path | None = Path(patches_file) if patches_file else None
        # persistent audit log — defaults to ~/.nselib/guardian_log.json
        self._log_file: Path = (
            Path(log_file) if log_file
            else Path.home() / ".nselib" / "guardian_log.json"
        )
        self._load_patches()

    # ------------------------------------------------------------------ #
    #  Persistent patches                                                #
    # ------------------------------------------------------------------ #

    def _load_patches(self) -> None:
        if not self._patches_file or not self._patches_file.exists():
            return
        try:
            data = json.loads(self._patches_file.read_text())
            self._patches       = data.get("patches", {})
            self._schema_remaps = data.get("schema_remaps", {})
            if data.get("cookie_ttl"):
                self._session._COOKIE_TTL = data["cookie_ttl"]
            loaded = len(self._patches)
            if loaded:
                print(f"[Guardian] Loaded {loaded} saved patch(es) from {self._patches_file}")
        except Exception as e:
            print(f"[Guardian] Could not load patches file: {e}")

    def _save_patches(self) -> None:
        if not self._patches_file:
            return
        try:
            self._patches_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "patches":       self._patches,
                "schema_remaps": self._schema_remaps,
                "cookie_ttl":    self._session._COOKIE_TTL,
                "saved_at":      datetime.now().isoformat(),
            }
            self._patches_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[Guardian] Could not save patches file: {e}")

    def _log(self, event: str, **kwargs) -> None:
        """Append one event to the persistent guardian_log.json."""
        entry = {"ts": datetime.now().isoformat(), "event": event, **kwargs}
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            log: list = []
            if self._log_file.exists():
                try:
                    log = json.loads(self._log_file.read_text())
                except Exception:
                    log = []
            log.append(entry)
            self._log_file.write_text(json.dumps(log, indent=2))
        except Exception as exc:
            print(f"[Guardian] Could not write log: {exc}")

    # ------------------------------------------------------------------ #
    #  Heal                                                               #
    # ------------------------------------------------------------------ #

    def guarded_get(self, path: str, params: dict | None = None) -> Any:
        """
        Drop-in replacement for NSESession.get().
        Automatically heals broken responses via Claude.
        """
        try:
            resp = self._apply_and_get(path, params)
        except NSEFileUnavailableError as e:
            resp = self._heal(path, params, {"error": "404", "url": str(e)})
            if isinstance(resp, dict):
                resp = self._check_and_heal_schema(path, resp)
            return resp

        if self._is_broken(resp):
            resp = self._heal(path, params, resp)

        if isinstance(resp, dict):
            resp = self._check_and_heal_schema(path, resp)

        return resp

    def _apply_and_get(self, path: str, params: dict | None) -> Any:
        patch = self._patches.get(path, {})
        effective_path = patch.get("new_path", path)
        merged_params  = {**(params or {}), **patch.get("params", {})}

        sess = self._session._s
        old_headers = dict(sess.headers)
        if patch.get("headers"):
            sess.headers.update(patch["headers"])

        try:
            return self._raw_get(effective_path, merged_params or None)
        finally:
            if patch.get("headers"):
                sess.headers.clear()
                sess.headers.update(old_headers)

    def _is_broken(self, resp: Any) -> bool:
        if resp is None:
            return True
        if isinstance(resp, dict) and not resp:
            return True
        if isinstance(resp, list) and not resp:
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Archive heal — guarded_archive + URL-pattern repair                #
    # ------------------------------------------------------------------ #

    def guarded_archive(
        self,
        url:    str,
        action: "Any | None" = None,
    ) -> Any:
        """
        Execute action(url) — an archive request against nsearchives.nseindia.com.

        On NSEFileUnavailableError (404), asks Brain to infer the new URL pattern,
        patches the calling method's source code, and retries with the corrected URL.

        action: any callable(str) -> Any.
                Defaults to self._session.get_raw (returns raw Response).
                Pass self._session.download_zip / download_file / download_gz
                when the caller needs a different download helper.

        Usage in archive methods::

            _dl = lambda u: self._session.download_zip(u, folder=Path(folder))
            return self._guardian.guarded_archive(url, _dl) if self._guardian else _dl(url)
        """
        import inspect
        caller = inspect.stack()[1]
        _action = action if action is not None else self._session.get_raw
        try:
            return _action(url)
        except NSEFileUnavailableError:
            # 404 on a historical date (>7 days ago) = holiday / non-trading day.
            # Don't call Brain to "fix" the URL pattern — those 404s are expected.
            if self._url_date_is_historical(url):
                raise
            new_url = self._heal_archive_url(url, caller.filename, caller.function)
            return _action(new_url)

    @staticmethod
    def _url_date_is_historical(url: str) -> bool:
        """True when the URL contains an embedded date >7 days ago."""
        import re as _re
        from datetime import date as _date
        today = _date.today()
        # Pattern 1: yyyymmdd (newer bhavcopy: BhavCopy_NSE_CM_0_0_0_20261023_...)
        for m in _re.finditer(r'(\d{4})(\d{2})(\d{2})', url):
            yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= yyyy <= 2099 and 1 <= mm <= 12 and 1 <= dd <= 31:
                try:
                    return (today - _date(yyyy, mm, dd)).days > 7
                except ValueError:
                    continue
        # Pattern 2: ddmmyyyy (most archive filenames: FOSett_prce_25102026.csv)
        for m in _re.finditer(r'(\d{2})(\d{2})(\d{4})', url):
            dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= yyyy <= 2099 and 1 <= mm <= 12 and 1 <= dd <= 31:
                try:
                    return (today - _date(yyyy, mm, dd)).days > 7
                except ValueError:
                    continue
        # Pattern 3: ddmmyy (2-digit year: PE_251026.csv, CMVOLT_251026.CSV)
        for m in _re.finditer(r'(\d{2})(\d{2})(\d{2})(?!\d)', url):
            dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                try:
                    return (today - _date(2000 + yy, mm, dd)).days > 7
                except ValueError:
                    continue
        return False

    def _heal_archive_url(
        self, url: str, caller_file: str, caller_name: str
    ) -> str:
        """Ask Brain to fix a broken archive URL and patch the source."""
        print(
            f"[Guardian] Archive 404 in {Path(caller_file).name}/{caller_name} "
            "— asking Claude to fix URL pattern…"
        )
        source = self._extract_method_source(caller_file, caller_name)
        prompt = (
            f"An NSE archive file returned 404 (file not found).\n"
            f"  URL: {url}\n"
            f"  Method: {caller_name} in {Path(caller_file).name}\n\n"
            f"Method source:\n{source}\n\n"
            f"NSE occasionally changes archive paths or filename formats "
            f"(e.g. folder rename, added prefix/suffix, date-format change).\n"
            f"Suggest the corrected URL for this specific request, and if the "
            f"URL construction pattern has changed provide the full corrected "
            f"method code so the pattern is fixed for all future calls.\n\n"
            f"Reply ONLY with JSON:\n"
            f'{{"new_url": "corrected-url-or-null", '
            f'"fixed_code": "corrected-method-or-null", '
            f'"explanation": "one sentence"}}'
        )
        fix_raw = self._brain.ask(prompt)
        fix     = self._parse_json(fix_raw) or {}
        new_url = fix.get("new_url", "")

        if fix.get("fixed_code") and source:
            self._patch_method_source(caller_file, source, fix["fixed_code"])

        if new_url:
            print(f"[Guardian] Archive URL healed: {fix.get('explanation', '')}")
            self._log(
                "heal_archive",
                url=url, new_url=new_url,
                method=caller_name,
                explanation=fix.get("explanation", ""),
            )
            return new_url

        raise NSEFileUnavailableError(url)

    def _extract_method_source(self, filepath: str, method_name: str) -> str:
        """Return the source of a method from a file, or '' on failure."""
        try:
            text = Path(filepath).read_text()
            m = re.search(
                rf"(    def {re.escape(method_name)}\b.*?)(?=\n    def |\Z)",
                text, re.DOTALL,
            )
            return m.group(1) if m else ""
        except Exception:
            return ""

    def _patch_method_source(
        self, filepath: str, old_source: str, new_code: str
    ) -> None:
        """Replace a method's source in-place."""
        try:
            text = Path(filepath).read_text()
            old  = old_source.rstrip()
            new  = "    " + new_code.strip().replace("\n", "\n    ")
            updated = text.replace(old, new, 1)
            if updated != text:
                Path(filepath).write_text(updated)
                print(f"[Guardian] Patched archive method in {Path(filepath).name}")
                self._log("patch_archive", filepath=filepath,
                          snippet=old_source[:80])
        except Exception as e:
            print(f"[Guardian] Could not patch archive source: {e}")

    def _heal(self, path: str, params: dict | None, bad_resp: Any) -> Any:
        print(f"[Guardian] '{path}' returned empty/error — asking Claude to diagnose…")

        # Build a compact catalogue summary for Brain to reason over
        catalogue = _load_catalogue()
        catalogue_lines = "\n".join(
            f"  {p}: {meta.get('purpose', '')} | method={meta.get('nselib_method')} | params={meta.get('params', [])}"
            for p, meta in catalogue.items()
        )
        catalogue_section = (
            f"\nKnown NSE endpoints (from live catalogue):\n{catalogue_lines}\n"
            if catalogue_lines else ""
        )

        prompt = (
            f"An NSE India API call returned an empty or error response.\n"
            f"  path:     {path}\n"
            f"  params:   {json.dumps(params or {})}\n"
            f"  response: {bad_resp!r}\n"
            f"{catalogue_section}\n"
            f"IMPORTANT: Paths are relative — do NOT include '/api/' prefix. "
            f"NSESession.get() prepends 'https://www.nseindia.com/api/' automatically.\n"
            f"Use the catalogue above to find the correct or alternative path. "
            f"If the failed path looks like a renamed version of a known endpoint "
            f"(e.g. 'corporates-x' vs 'corporate-x', 'historical' vs 'historicalOR'), "
            f"suggest the corrected path.\n\n"
            f"Reply with ONLY valid JSON:\n"
            f"{{\n"
            f'  "new_path": null_or_string,\n'
            f'  "extra_headers": {{}},\n'
            f'  "extra_params": {{}},\n'
            f'  "cookie_ttl_seconds": null_or_int,\n'
            f'  "explanation": "one sentence"\n'
            f"}}"
        )

        fix_raw = self._brain.ask(prompt)
        fix = self._parse_json(fix_raw) or {}

        patch: dict = {}
        if fix.get("new_path"):
            patch["new_path"] = fix["new_path"]
        if fix.get("extra_headers"):
            patch["headers"] = fix["extra_headers"]
        if fix.get("extra_params"):
            patch["params"] = fix["extra_params"]

        with self._lock:
            if patch:
                self._patches[path] = patch
            if fix.get("cookie_ttl_seconds"):
                self._session._COOKIE_TTL = fix["cookie_ttl_seconds"]
            entry = {
                "ts":          datetime.now().isoformat(),
                "path":        path,
                "explanation": fix.get("explanation", ""),
                "patch":       patch,
            }
            self._heal_log.append(entry)
            self._save_patches()

        self._log("heal", path=path, patch=patch,
                  explanation=fix.get("explanation", ""))
        print(f"[Guardian] Patch applied: {fix.get('explanation', 'unknown')}")
        return self._apply_and_get(path, params)

    # ------------------------------------------------------------------ #
    #  Adapt — schema drift detection + healing                          #
    # ------------------------------------------------------------------ #

    def _check_and_heal_schema(self, path: str, resp: dict) -> dict:
        """
        1. Apply any previously saved key remaps (fast path, no LLM).
        2. Detect drift vs last-seen keys.
        3. Fuzzy-match removed keys → new keys; fall back to Brain for
           anything that can't be resolved heuristically.
        4. Persist the remap so future calls are transparent.
        Returns the (possibly remapped) response dict.
        """
        # Apply saved remaps so existing nselib code keeps working
        saved = self._schema_remaps.get(path)
        if saved:
            resp = self._remap_dict(resp, saved)

        keys = set(resp.keys())
        prev = self._schema.get(path)

        if prev is not None and keys != prev:
            removed = prev - keys
            added   = keys - prev

            if removed:
                remap: dict[str, str] = {}
                unmatched: list[str] = []

                for old_k in removed:
                    match = self._fuzzy_key_match(old_k, added)
                    if match:
                        remap[match] = old_k   # rename new API key → old nselib key
                        print(f"[Guardian] Schema remap '{path}': '{match}' → '{old_k}'")
                    else:
                        unmatched.append(old_k)

                if unmatched and self._brain:
                    brain_remap = self._heal_schema_brain(path, unmatched, added, resp)
                    remap.update(brain_remap)
                    unmatched = [k for k in unmatched if k not in {v for v in brain_remap.values()}]

                if unmatched:
                    print(f"[Guardian] Schema drift on '{path}' — unmapped keys: {sorted(unmatched)}")

                if remap:
                    with self._lock:
                        merged = {**self._schema_remaps.get(path, {}), **remap}
                        self._schema_remaps[path] = merged
                        self._save_patches()
                    self._log("adapt", path=path, remap=remap)
                    resp = self._remap_dict(resp, remap)

            elif added:
                # Only new keys — no breakage, just log
                print(f"[Guardian] Schema expanded on '{path}': new keys {sorted(added)}")

        self._schema[path] = set(resp.keys())
        return resp

    def _heal_schema_brain(
        self,
        path: str,
        removed: list[str],
        added:   set[str],
        sample:  dict,
    ) -> dict[str, str]:
        """Ask Brain to map ambiguous removed keys to their new equivalents."""
        prompt = (
            f"NSE API path '{path}' response changed.\n"
            f"Keys nselib expects but are now missing: {sorted(removed)}\n"
            f"New keys in the response: {sorted(added)}\n"
            f"Full response keys: {sorted(sample.keys())}\n\n"
            f"For each missing key, find its replacement in the new keys "
            f"(or null if truly gone). Reply ONLY with JSON:\n"
            f"{{\"<new_api_key>\": \"<old_nselib_key>\", ...}}"
        )
        raw  = self._brain.ask(prompt)
        data = self._parse_json(raw) or {}
        result = {}
        for new_k, old_k in data.items():
            if old_k and isinstance(old_k, str) and new_k in added:
                result[new_k] = old_k
                print(f"[Guardian] Brain schema remap '{path}': '{new_k}' → '{old_k}'")
        return result

    @staticmethod
    def _remap_dict(resp: dict, remap: dict[str, str]) -> dict:
        """Rename keys in resp according to remap {current_key: desired_key}."""
        return {remap.get(k, k): v for k, v in resp.items()}

    @staticmethod
    def _fuzzy_key_match(target: str, candidates: set[str]) -> str | None:
        """
        Return the best match for `target` from `candidates`, or None.
        Tries (in order): normalised exact, substring containment, edit distance ≤ 2.
        """
        if not candidates:
            return None

        def norm(k: str) -> str:
            return re.sub(r"[_\-]", "", k.lower())

        t = norm(target)

        # 1. normalised exact match
        for c in candidates:
            if norm(c) == t:
                return c

        # 2. one normalised form contains the other
        for c in candidates:
            cn = norm(c)
            if t in cn or cn in t:
                return c

        # 3. edit distance ≤ 2 (Levenshtein)
        def _lev(a: str, b: str) -> int:
            dp = list(range(len(b) + 1))
            for ch_a in a:
                prev, dp[0] = dp[0], dp[0] + 1
                for j, ch_b in enumerate(b, 1):
                    prev, dp[j] = dp[j], prev if ch_a == ch_b else 1 + min(prev, dp[j], dp[j-1])
            return dp[len(b)]

        best, best_d = None, 3
        for c in candidates:
            d = _lev(t, norm(c))
            if d < best_d:
                best, best_d = c, d
        return best

    # ------------------------------------------------------------------ #
    #  Discover — archive files (daily-reports API)                       #
    # ------------------------------------------------------------------ #

    _ARCHIVE_SEGMENTS = ("CM", "FO", "WDM", "EGR")
    _SKIP_EXTENSIONS  = {".dat", ".doc", ".docx", ".xls", ".xlsx"}

    def discover_archive(
        self,
        segments: tuple[str, ...] | None = None,
        nselib_dir: str | Path | None = None,
    ) -> list[dict]:
        """
        Discover NSE daily-report archive files not yet implemented in nselib.

        Pipeline:
          1. Query NSE_API_IN/api/daily-reports?key=<seg> for each segment
          2. For each file, check if the URL path signature already appears
             anywhere in the module source (= covered) or not (= new)
          3. For each new file, download a sample to understand the format
          4. Ask Brain to generate the full archive method using the
             standard 5-day retry pattern
          5. Record discoveries so apply() + verify() can handle them

        Returns the same list[dict] format as discover() so apply() can
        run the combined results without knowing which source they came from.
        """
        import gzip as _gz
        import io as _io2
        import zipfile as _zf

        segs    = segments or self._ARCHIVE_SEGMENTS
        base_in = "https://www.nseindia.in/api"
        modules_dir = (
            Path(nselib_dir) / "nselib" / "modules"
            if nselib_dir
            else Path(__file__).parent / "modules"
        )

        # Build one big string of all module source to search for coverage
        module_source = ""
        for f in modules_dir.glob("*.py"):
            try:
                module_source += f.read_text()
            except Exception:
                pass

        discoveries: list[dict] = []

        for seg in segs:
            print(f"[Guardian/archive] Checking segment {seg}…")
            try:
                r = self._session._s.get(
                    f"{base_in}/daily-reports?key={seg}", timeout=15
                )
                if r.status_code != 200:
                    print(f"[Guardian/archive] {seg}: HTTP {r.status_code}")
                    continue
                data = r.json()
            except Exception as exc:
                print(f"[Guardian/archive] {seg}: fetch error — {exc}")
                continue

            files = data.get("CurrentDay", []) or data.get("PreviousDay", [])

            for item in files:
                filename  = item.get("fileActlName", "")
                file_path = item.get("filePath", "").rstrip("/")
                display   = item.get("displayName", "")
                full_url  = f"{file_path}/{filename}"

                if not filename or not file_path:
                    continue

                # Skip known-binary formats and SPAN files (proprietary binary)
                suffix = Path(filename).suffix.lower()
                if suffix in self._SKIP_EXTENSIONS:
                    continue
                if "span" in display.lower() or "span" in filename.lower():
                    continue

                # Coverage check — the most reliable signal is the archive
                # directory path, which appears verbatim in every method's URL
                # f-string. Strip the host to get the path, then check whether
                # the directory portion appears anywhere in the module source.
                import re as _re
                host = "https://nsearchives.nseindia.com"
                arch_path = full_url.replace(host, "")           # e.g. /archives/nsccl/sett/FOSett_prce_26102026.csv
                arch_dir  = arch_path.rsplit("/", 1)[0]          # e.g. /archives/nsccl/sett
                # Also check the file name prefix (invariant part before first date run)
                file_prefix = _re.split(r"\d{6,}", filename)[0]  # e.g. "FOSett_prce_"

                # Covered if the full archive directory path appears in source,
                # OR if a long file prefix (>=6 chars) appears in source.
                covered = (
                    (len(arch_dir) >= 8 and arch_dir.lower() in module_source.lower())
                    or (len(file_prefix) >= 6 and file_prefix.lower() in module_source.lower())
                )
                if covered:
                    continue

                print(f"[Guardian/archive]   NEW: {display} ({filename})")

                # Sample the file to understand its format
                sample_text = self._sample_archive_file(full_url, suffix)
                if sample_text is None:
                    print(f"[Guardian/archive]   skipping {filename} — could not download sample")
                    continue

                # Determine which module it belongs to
                module = self._archive_module_for(seg, display, filename)

                # Ask Brain to generate the method
                method_code, method_name = self._generate_archive_method(
                    full_url, filename, file_path, display, seg, module, sample_text
                )
                if not method_code or not method_name:
                    print(f"[Guardian/archive]   Brain returned no code for {filename}")
                    continue

                disc = {
                    "path":        full_url,
                    "module":      module,
                    "method_name": method_name,
                    "method_code": method_code,
                    "_source":     "archive",
                    "_display":    display,
                }
                discoveries.append(disc)
                print(f"[Guardian/archive]   Generated: {method_name}() → {module}.py")

        with self._lock:
            self._discoveries.extend(discoveries)

        self._log(
            "discover_archive",
            segments=list(segs),
            new_methods=[d["method_name"] for d in discoveries],
        )
        return discoveries

    def _sample_archive_file(self, url: str, suffix: str) -> str | None:
        """Download up to 3 KB of an archive file; decompress if needed."""
        import gzip as _gz, io as _io2, zipfile as _zf
        try:
            r = self._session._s.get(url, timeout=30, stream=True)
            if r.status_code != 200 or b"DOCTYPE" in r.content[:50]:
                return None
            raw = r.content[:65536]  # at most 64 KB
            if suffix == ".gz":
                try:
                    with _gz.open(_io2.BytesIO(raw)) as f:
                        return f.read(3000).decode("utf-8", errors="replace")
                except Exception:
                    return None
            if suffix == ".zip":
                try:
                    with _zf.ZipFile(_io2.BytesIO(raw)) as z:
                        names = [n for n in z.namelist() if n.endswith(".csv")]
                        if not names:
                            return None
                        return z.read(names[0])[:3000].decode("utf-8", errors="replace")
                except Exception:
                    return None
            return raw[:3000].decode("utf-8", errors="replace")
        except Exception:
            return None

    def _archive_module_for(self, seg: str, display: str, filename: str) -> str:
        dl = display.lower()
        fn = filename.lower()
        if seg in ("WDM",):
            return "downloads"
        if seg in ("EGR",):
            return "downloads"
        if "haircut" in dl or "var" in dl or "margin" in dl or "surveillance" in dl:
            return "downloads"
        if "bhavcopy" in dl or "bhav" in fn or "pe ratio" in dl or "week52" in dl:
            return "downloads"
        if "participant" in dl or "ban" in dl or "mwpl" in dl or "oi" in dl:
            return "fno"
        if "settlement" in dl or "volatility" in dl or "exposure" in dl or "delta" in dl:
            return "fno"
        if "latency" in dl:
            return "fno" if seg == "FO" else "downloads"
        if "market activity" in dl or "turnover" in dl:
            return "fno" if seg == "FO" else "market"
        if "contract" in dl or "spread" in dl or "option" in dl:
            return "fno"
        if seg == "FO":
            return "fno"
        return "downloads"

    def _generate_archive_method(
        self,
        full_url: str,
        filename: str,
        file_path: str,
        display: str,
        seg: str,
        module: str,
        sample: str,
    ) -> tuple[str, str]:
        """Ask Brain to write the archive method. Returns (code, name)."""
        import re as _re

        # Extract URL pattern by replacing date digits with a placeholder
        pattern_url = _re.sub(r"\d{8}", "{ddmmyyyy}", full_url)
        pattern_url = _re.sub(r"\d{6}", "{ddmmyy}", pattern_url)
        pattern_url = _re.sub(
            r"\d{2}-[A-Z]{3}-\d{4}", "{DD-MON-YYYY}", pattern_url
        )

        reference = '''    def fo_settlement_prices(self, trade_date: date | None = None) -> list[dict]:
        """Daily MTM settlement prices for all F&O contracts."""
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            url = f"{_ARCHIVE}/archives/nsccl/sett/FOSett_prce_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        sym = (row.get("UNDERLYING") or "").strip()
                        if not sym:
                            continue
                        out.append({
                            "date":             (row.get("DATE") or "").strip(),
                            "instrument":       (row.get("INSTRUMENT") or "").strip(),
                            "underlying":       sym,
                            "expiry":           (row.get("EXPIRY DATE") or "").strip(),
                            "settlement_price": (row.get("MTM SETTLEMENT PRICE") or "").strip(),
                        })
                    return out
            except Exception:
                continue
        return []'''

        prompt = f"""You are generating a Python method for the nselib library to fetch an NSE India archive file.

NSE file metadata:
  Display name : {display}
  Segment      : {seg}
  Full URL     : {full_url}
  URL pattern  : {pattern_url}
  Target module: {module}

First 3000 bytes of the actual file:
```
{sample[:2000]}
```

Reference pattern (use this exact structure — do NOT use self._session.get(), always use self._session._s.get()):
```python
{reference}
```

Rules:
1. Use `self._session._s.get(url, timeout=15)` — never `self._session.get()`.
2. Always add `if check.weekday() >= 5: continue` inside the retry loop.
3. Always check `b"DOCTYPE" not in r.content[:50]` before parsing.
4. For .gz files: `import gzip as _gz; with _gz.open(io.BytesIO(r.content)) as f: text = f.read().decode()`
5. For .zip files: `import zipfile as _zf; with _zf.ZipFile(io.BytesIO(r.content)) as z:` — list files, read CSVs.
6. For no-header CSVs: use `csv.reader` and assign column names manually.
7. Build the URL by replacing the date in the pattern URL above. Use `check.strftime()`.
8. Use `_ARCHIVE = "https://nsearchives.nseindia.com"` — it's already defined at module level.
9. The method parameter should be `dt: date | None = None` for CM/WDM/EGR or `trade_date: date | None = None` for FO.
10. Return `[]` or `{{}}` on failure — never raise.
11. Strip every string value: `(row.get("COL") or "").strip()`.
12. Choose a descriptive snake_case method name that doesn't already exist.

Reply with ONLY a JSON object:
{{"method_name": "snake_case_name", "method_code": "def method_name(self, ...):\\n    ..."}}
"""
        raw = self._brain.ask(prompt)
        parsed = self._parse_json(raw)
        if not isinstance(parsed, dict):
            return "", ""
        name = (parsed.get("method_name") or "").strip()
        code = (parsed.get("method_code") or "").strip()
        if not name or not code:
            return "", ""
        # strip markdown fences if Brain wrapped them
        import re as _re
        code = _re.sub(r"^```python\s*|^```\s*|```$", "", code, flags=_re.MULTILINE).strip()
        return code, name

    # ------------------------------------------------------------------ #
    #  Discover — live API endpoints                                       #
    # ------------------------------------------------------------------ #

    def discover(self, focus: str | None = None) -> list[dict]:
        """
        Ask Claude to suggest NSE API endpoints not yet in nselib, then
        probe each candidate directly to confirm it's live.
        Works on any IP — no JS bundle scraping needed.
        Returns list of discovery dicts with confirmed live endpoints.

        Args:
            focus: Optional area to target, e.g.
                   "insider trading, block/bulk deals, corporate governance".
                   When None, uses the default broad discovery prompt.
        """
        print("[Guardian] Asking Claude for undiscovered NSE endpoints…")

        focus_line = (
            f"Focus specifically on: {focus}"
            if focus else
            "Focus on: FII/DII data, put-call ratio, OI analysis, "
            "52-week data, circuit breakers, deliverable positions, VIX, "
            "currency/commodity derivatives that definitely exist."
        )

        prompt = (
            f"You are an expert on NSE India's undocumented REST API at "
            f"https://www.nseindia.com/api/\n\n"
            f"nselib already knows these paths:\n"
            f"{json.dumps(sorted(_KNOWN_PATHS), indent=2)}\n\n"
            f"List the TOP 20 most useful NSE API paths that nselib is likely MISSING. "
            f"Be specific and high-confidence only — paths you are fairly certain exist "
            f"(not guesses). {focus_line}\n\n"
            f"Reply ONLY with a JSON array of up to 20 candidate paths "
            f"(relative, no /api/ prefix):\n"
            f'["path1", "path2", ...]'
        )

        raw = self._brain.ask(prompt)
        candidates = self._parse_json(raw)
        if not isinstance(candidates, list):
            print("[Guardian] No candidates returned.")
            return []

        candidates = [p for p in candidates if isinstance(p, str) and p not in _KNOWN_PATHS]
        print(f"[Guardian] {len(candidates)} candidate(s) to probe: {candidates}")

        # Probe each candidate directly — only keep ones that return real data
        live: list[str] = []
        for path in candidates:
            try:
                resp = self._raw_get(path, None)
                if resp and not self._is_broken(resp):
                    live.append(path)
                    print(f"[Guardian]   ✓ live: {path}")
            except Exception:
                pass  # 404 or error — skip

        if not live:
            print("[Guardian] No live new endpoints confirmed.")
            return []

        print(f"[Guardian] Confirmed {len(live)} new endpoint(s). Generating methods…")

        gen_prompt = (
            f"These NSE API endpoints are confirmed live and not yet in nselib:\n"
            f"{json.dumps(live, indent=2)}\n\n"
            f"For each one, generate a Python mixin method.\n"
            f"IMPORTANT RULES:\n"
            f"1. Paths are relative — do NOT include '/api/' prefix.\n"
            f"2. ALWAYS call self._guardian.guarded_get(path, params) instead of "
            f"self._session.get(path, params). This ensures every new method gets "
            f"automatic self-healing, schema drift detection, and retry on failure.\n"
            f"3. If self._guardian is None, fall back to self._session.get(path, params).\n"
            f"Example call pattern:\n"
            f"  data = (self._guardian.guarded_get if self._guardian else self._session.get)"
            f"(\"the-path\", params)\n\n"
            f"Reply with ONLY a valid JSON array:\n"
            f"[\n"
            f"  {{\n"
            f'    "path": "the-endpoint-path",\n'
            f'    "module": "quotes|indices|market|history|corporate|fno|downloads",\n'
            f'    "method_name": "snake_case_name",\n'
            f'    "method_code": "def method_name(self, ...):\\n    ..."\n'
            f"  }}\n"
            f"]"
        )

        raw2 = self._brain.ask(gen_prompt)
        discoveries = self._parse_json(raw2)
        if not isinstance(discoveries, list):
            discoveries = []

        with self._lock:
            self._discoveries.extend(discoveries)
            _KNOWN_PATHS.update(d["path"] for d in discoveries if "path" in d)

        self._log("discover",
                  live_endpoints=live,
                  methods_generated=[d.get("method_name") for d in discoveries])
        return discoveries

    def apply(self, nselib_dir: str | Path, nse_instance=None) -> list[str]:
        """
        Write methods generated by discover() into the right module files,
        then automatically verify each one. Methods that fail and cannot be
        fixed are removed from the file before returning.

        Ensures every generated method routes through guarded_get() so it
        gets automatic self-healing and schema drift detection from day one.
        Returns a list of files that were modified.
        """
        base     = Path(nselib_dir) / "nselib" / "modules"
        modified: list[str] = []

        for d in self._discoveries:
            module = d.get("module", "")
            code   = d.get("method_code", "").strip()
            if not module or not code:
                continue

            target = base / f"{module}.py"
            if not target.exists():
                print(f"[Guardian] Module file not found: {target}")
                continue

            current = target.read_text()
            if d.get("method_name") and d["method_name"] in current:
                print(f"[Guardian] '{d['method_name']}' already exists in {module}.py — skipping.")
                continue

            # Archive methods use self._session._s.get() directly (intentional —
            # bypasses Akamai check, uses retry loop internally).
            # Only wrap bare self._session.get() calls (live API) with guarded_get.
            if d.get("_source") != "archive":
                code = code.replace(
                    "self._session.get(",
                    "(self._guardian.guarded_get if self._guardian else self._session.get)(",
                )

            indented = "\n    ".join(code.splitlines())
            updated  = current.rstrip("\n") + f"\n\n    {indented}\n"
            target.write_text(updated)
            modified.append(str(target))
            self._log("apply", method=d.get("method_name"),
                      module=module, path=d.get("path"))
            src_tag = " (archive)" if d.get("_source") == "archive" else " (guardian-wrapped)"
            print(f"[Guardian] Added '{d.get('method_name')}' to {module}.py{src_tag}")

        if self._discoveries:
            print(f"[Guardian] Verifying {len(self._discoveries)} new method(s)…")
            results = self.verify(nselib_dir, nse_instance=nse_instance)
            # write status back onto each discovery so HealthChecker can read it
            verify_map = {r["method"]: r["status"] for r in results}
            for d in self._discoveries:
                d["_verify_status"] = verify_map.get(d.get("method_name"), "skipped")
            passed  = sum(1 for r in results if r["status"] in ("pass", "fixed"))
            removed = sum(1 for r in results if r["status"] == "removed")
            failed  = sum(1 for r in results if r["status"] == "failed")
            print(f"[Guardian] Verification: {passed} pass, {removed} removed, {failed} failed")

        return modified

    def verify(self, nselib_dir: str | Path, nse_instance=None) -> list[dict]:
        """
        Test every method written by apply(). For each one:
          1. Call it with no args (or minimal sample args)
          2. If it returns non-empty data → PASS
          3. If it fails or returns empty → ask Claude to fix the method and rewrite it

        Returns a list of {method, status, detail} dicts.
        """
        import importlib, inspect, sys as _sys

        base    = Path(nselib_dir) / "nselib" / "modules"
        results = []

        # reuse a live NSE instance if provided, otherwise use session directly
        from .nse import NSE as _NSE
        nse = nse_instance or _NSE.__new__(_NSE)
        if nse_instance is None:
            nse._session = self._session
            nse._brain   = self._brain
            nse._guardian = self

        for d in self._discoveries:
            method_name = d.get("method_name")
            module      = d.get("module")
            path        = d.get("path", "")
            if not method_name or not module:
                continue

            # reload the module so the newly written method is importable
            mod_path = f"nselib.modules.{module}"
            try:
                if mod_path in _sys.modules:
                    importlib.reload(_sys.modules[mod_path])
            except Exception:
                pass

            method = getattr(nse, method_name, None)
            if method is None:
                results.append({"method": method_name, "status": "missing",
                                "detail": "method not found on NSE instance after reload"})
                self._log("verify", method=method_name, status="missing")
                continue

            # call with no args; catch any error
            try:
                resp = method()
                if resp and not self._is_broken(resp):
                    results.append({"method": method_name, "status": "pass",
                                    "detail": f"returned {type(resp).__name__}"})
                    self._log("verify", method=method_name, status="pass")
                    print(f"[Guardian] ✓ {method_name}")
                    continue
                else:
                    raise ValueError("empty response")
            except Exception as exc:
                print(f"[Guardian] ✗ {method_name} failed: {exc} — asking Claude to fix…")

                target      = base / f"{module}.py"
                last_error  = str(exc)
                last_code   = d.get("method_code", "")
                status      = "failed"
                detail      = f"unfixable: {last_error}"
                max_attempts = 3

                for attempt in range(max_attempts):
                    attempt_label = f"attempt {attempt + 1}/{max_attempts}"
                    fix_prompt = (
                        f"The nselib method `{method_name}` for NSE path `{path}` "
                        f"failed when called with no arguments ({attempt_label}).\n"
                        f"Error: {last_error}\n\n"
                        f"Current (broken) implementation:\n{last_code}\n\n"
                        f"Fix the method so it works with sensible defaults for any "
                        f"required parameters. Use guarded_get pattern. "
                        f"Reply with ONLY the corrected method code (no JSON wrapper)."
                    )
                    fixed_code = self._brain.ask(fix_prompt).strip()
                    fixed_code = re.sub(r"^```python\s*|^```\s*|```$", "",
                                        fixed_code, flags=re.MULTILINE).strip()

                    if not fixed_code or not target.exists():
                        # no fix generated — remove the broken method from the file
                        if target.exists():
                            file_text    = target.read_text()
                            bad_indented = "\n    ".join(last_code.strip().splitlines())
                            cleaned      = file_text.replace(f"\n\n    {bad_indented}\n", "", 1)
                            if cleaned != file_text:
                                target.write_text(cleaned)
                                print(f"[Guardian] ✗ {method_name} — no fix generated, removed from file")
                        detail = f"removed: no fix generated ({last_error})"
                        status = "removed"
                        break

                    old_indented = "\n    ".join(last_code.strip().splitlines())
                    new_indented = "\n    ".join(fixed_code.splitlines())
                    current      = target.read_text()
                    updated      = current.replace(f"    {old_indented}", f"    {new_indented}", 1)
                    if updated == current:
                        detail = f"fix generated but could not splice into file ({last_error})"
                        break

                    target.write_text(updated)
                    d["method_code"] = fixed_code
                    print(f"[Guardian] Spliced fix for {method_name} ({attempt_label}) — re-testing…")

                    try:
                        if mod_path in _sys.modules:
                            importlib.reload(_sys.modules[mod_path])
                        method2 = getattr(nse, method_name, None)
                        resp2   = method2() if method2 else None
                        if resp2 and not self._is_broken(resp2):
                            detail = f"fixed and verified ({attempt_label})"
                            status = "fixed"
                            print(f"[Guardian] ✓ {method_name} (fixed on {attempt_label})")
                            break
                        else:
                            last_error = "empty response after fix"
                            last_code  = fixed_code
                            print(f"[Guardian] ✗ {method_name} still empty ({attempt_label}), retrying…")
                    except Exception as retest_exc:
                        last_error = str(retest_exc)
                        last_code  = fixed_code
                        print(f"[Guardian] ✗ {method_name} still failing ({attempt_label}): "
                              f"{retest_exc} — retrying…")
                else:
                    # all attempts exhausted — remove the method from the file
                    print(f"[Guardian] ✗ {method_name} could not be fixed after {max_attempts} "
                          f"attempts — removing from file")
                    file_text    = target.read_text()
                    bad_indented = "\n    ".join(last_code.strip().splitlines())
                    file_text    = file_text.replace(f"\n\n    {bad_indented}\n", "", 1)
                    target.write_text(file_text)
                    detail = f"removed: {max_attempts} fix attempts failed — {last_error}"
                    status = "removed"

                results.append({"method": method_name, "status": status, "detail": detail})
                self._log("verify", method=method_name, status=status,
                          error=str(exc), detail=detail)

        return results

    def watch(self, interval: int = 3600, nselib_dir: str | Path | None = None) -> None:
        """
        Run discover() + discover_archive() in a background thread every
        `interval` seconds. New endpoints are printed; call apply() to write them.
        """
        def _loop():
            while True:
                time.sleep(interval)
                found  = self.discover()
                found += self.discover_archive(nselib_dir=nselib_dir)
                if found:
                    names = [d.get("method_name", d.get("path")) for d in found]
                    print(f"[Guardian] New methods ready to apply: {names}")

        t = threading.Thread(target=_loop, daemon=True, name="guardian-discover")
        t.start()
        print(f"[Guardian] Discovery thread started — checking every {interval}s.")

    # ------------------------------------------------------------------ #
    #  Data pipeline health — bhavcopy + constituents                    #
    # ------------------------------------------------------------------ #

    def check_health(self) -> dict:
        """
        Check the health of both data pipelines.

        Returns a dict with keys 'bhav' and 'constituents', each containing:
          - status: 'ok' or 'stale'
          - last_date / last_snapshot: most recent date found
          - detail: human-readable explanation
        """
        import csv
        from datetime import date, timedelta
        from .modules.indices import _STORE, _BHAV_DIR

        result = {}

        # --- bhavcopy ---
        out_file = _BHAV_DIR / "constituents_1y.csv"
        expected = (date.today() - timedelta(days=1)).isoformat()
        if not out_file.exists():
            result["bhav"] = {"status": "stale", "last_date": None,
                              "detail": "constituents_1y.csv not found"}
        else:
            with open(out_file, newline="") as f:
                dates = [r["TradDt"] for r in csv.DictReader(f) if r.get("TradDt")]
            last = max(dates) if dates else None
            if last and last >= expected:
                result["bhav"] = {"status": "ok", "last_date": last,
                                  "detail": f"up to date ({last})"}
            else:
                result["bhav"] = {"status": "stale", "last_date": last,
                                  "detail": f"last date {last}, expected >= {expected}"}

        # --- constituents ---
        import json as _json
        comp = _STORE / "Comprehensive_latest.json"
        if not comp.exists():
            result["constituents"] = {"status": "stale", "last_snapshot": None,
                                      "detail": "Comprehensive_latest.json not found"}
        else:
            data = _json.loads(comp.read_text())
            snap_date = data.get("date")
            n_idx     = data.get("total_indices", 0)
            n_stocks  = data.get("total_stocks", 0)
            if snap_date and n_idx >= 60:
                result["constituents"] = {
                    "status": "ok", "last_snapshot": snap_date,
                    "detail": f"{n_idx} indices, {n_stocks} stocks as of {snap_date}",
                }
            else:
                result["constituents"] = {
                    "status": "stale", "last_snapshot": snap_date,
                    "detail": f"only {n_idx} indices found (expected ≥ 60)",
                }

        for name, info in result.items():
            icon = "✓" if info["status"] == "ok" else "✗"
            print(f"[Guardian] {icon} {name}: {info['detail']}")

        return result

    def heal_data(self, nse_instance=None) -> dict:
        """
        Fix any stale data pipelines found by check_health().

        Pass an NSE instance to reuse its session; if omitted, a fresh one
        is created using the Guardian's own session.

        Returns the health dict after healing.
        """
        health = self.check_health()

        # import here to avoid circular imports at module load
        from .nse import NSE as _NSE

        nse = nse_instance or _NSE.__new__(_NSE)
        if nse_instance is None:
            nse._session = self._session
            nse._brain   = self._brain

        if health.get("bhav", {}).get("status") == "stale":
            print("[Guardian] Healing bhavcopy…")
            try:
                nse.fetch_constituent_bhav_history(workers=4)
                self._log("data_heal", pipeline="bhav", status="healed")
                print("[Guardian] Bhavcopy healed.")
            except Exception as exc:
                self._log("data_heal", pipeline="bhav", status="failed", error=str(exc))
                print(f"[Guardian] Bhavcopy heal failed: {exc}")

        if health.get("constituents", {}).get("status") == "stale":
            print("[Guardian] Healing constituents…")
            try:
                nse.snapshot_all_constituents(workers=4)
                self._log("data_heal", pipeline="constituents", status="healed")
                print("[Guardian] Constituents healed.")
            except Exception as exc:
                self._log("data_heal", pipeline="constituents", status="failed", error=str(exc))
                print(f"[Guardian] Constituents heal failed: {exc}")

        return self.check_health()

    # ------------------------------------------------------------------ #
    #  Reporting                                                           #
    # ------------------------------------------------------------------ #

    def report(self) -> dict:
        """Full summary of what the Guardian has done this session."""
        return {
            "heals":       self._heal_log,
            "patches":     self._patches,
            "discoveries": self._discoveries,
            "schema":      {p: sorted(k) for p, k in self._schema.items()},
            "data_health": self.check_health(),
        }

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_json(text: str) -> Any:
        """Extract and parse JSON from Claude's response (may be wrapped in markdown)."""
        try:
            return json.loads(text)
        except Exception:
            pass
        # Try extracting from ```json ... ``` or bare { } / [ ]
        for pattern in (r"```json\s*([\s\S]+?)```", r"(\{[\s\S]+\})", r"(\[[\s\S]+\])"):
            m = re.search(pattern, text)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    pass
        return None
