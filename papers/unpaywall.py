from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from papers.cache import cache_root
from papers.fetch import TIMEOUT_SEC, FetchError, user_agent

UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
ERROR_TTL_SEC = 120


class LookupError(Exception):
    pass


@dataclass
class Lookup:
    is_oa: bool
    pdf_url: str | None
    title: str
    journal: str
    year: int | None
    license: str | None
    version: str | None
    # (url, license, version) for every OA PDF Unpaywall knows, best first.
    locations: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    @property
    def pdf_urls(self) -> list[str]:
        return [u for u, _, _ in self.locations]


def _locations(payload: dict) -> list[tuple[str, str | None, str | None]]:
    """Every OA PDF URL Unpaywall knows, repository copies (PMC etc.) first.

    Publisher sites (nature.com, cell.com, oup.com) often serve HTML or 403 to
    scripts, so we want the repository copy tried before them.
    """
    locs = payload.get("oa_locations") or []
    if not isinstance(locs, list):
        locs = []
    best = payload.get("best_oa_location")
    if isinstance(best, dict):
        locs = [best] + locs
    repo: list[tuple[str, str | None, str | None]] = []
    pub: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        url = loc.get("url_for_pdf")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)
        item = (url, loc.get("license") or None, loc.get("version") or None)
        (repo if loc.get("host_type") == "repository" else pub).append(item)
    return repo + pub




def _error_path() -> Path:
    return cache_root() / "unpaywall-errors.json"


def _load_errors() -> dict:
    path = _error_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_errors(data: dict) -> None:
    path = _error_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def recent_error(doi: str) -> bool:
    stamp = _load_errors().get(doi)
    if not isinstance(stamp, (int, float)):
        return False
    return (time.time() - stamp) < ERROR_TTL_SEC


def remember_error(doi: str) -> None:
    data = _load_errors()
    data[doi] = time.time()
    _save_errors(data)


def clear_error(doi: str) -> None:
    data = _load_errors()
    if doi in data:
        data.pop(doi, None)
        _save_errors(data)


def lookup(doi: str, mailto: str) -> Lookup:
    if recent_error(doi):
        raise LookupError("cached unpaywall error")
    url = UNPAYWALL.format(doi=urllib.parse.quote(doi, safe=""))
    url = url + "?" + urllib.parse.urlencode({"email": mailto})
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(mailto)},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status != 200:
                remember_error(doi)
                raise LookupError(f"http {resp.status}")
            payload = json.loads(resp.read().decode("utf-8"))
    except LookupError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        remember_error(doi)
        raise LookupError(str(exc)) from exc
    if not isinstance(payload, dict):
        remember_error(doi)
        raise LookupError("bad payload")
    clear_error(doi)
    loc = payload.get("best_oa_location") or {}
    if not isinstance(loc, dict):
        loc = {}
    locations = _locations(payload)
    pdf_url = locations[0][0] if locations else None
    year = _year(payload)
    return Lookup(
        is_oa=bool(payload.get("is_oa") and pdf_url),
        pdf_url=pdf_url,
        title=str(payload.get("title") or "").strip(),
        journal=str(payload.get("journal_name") or "").strip(),
        year=year,
        license=locations[0][1] if locations else (loc.get("license") or None),
        version=locations[0][2] if locations else (loc.get("version") or None),
        locations=locations,
    )


def _year(payload: dict) -> int | None:
    raw = payload.get("year")
    if isinstance(raw, int):
        return raw
    published = payload.get("published_date") or payload.get("published_print") or ""
    if isinstance(published, str) and len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None
