"""bioRxiv / medRxiv resolver via Cold Spring Harbor's own API.

Both servers share the 10.1101/ DOI prefix. The details endpoint returns
every posted version with its server, so we can fetch the newest PDF from a
canonical URL instead of guessing v1 on each host.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from papers.cache import TEXT_FLOOR, meta_path, pdf_path, text_path, write_meta
from papers.extract import write_text
from papers.fetch import FetchError, download_pdf, fetch_bytes

API_URL = "https://api.biorxiv.org/details/{server}/{doi}"
PDF_URL = "https://www.{server}.org/content/{doi}v{version}.full.pdf"
SERVERS = ("biorxiv", "medrxiv")
RETRY_GAP_SEC: float = 2.0

_PREFIX = re.compile(r"^10\.1101/\d")


def resolve(doi: str, mailto: str) -> bool | None:
    """Resolve a bioRxiv or medRxiv preprint through the CSHL API.

    Returns None when the DOI is not a 10.1101/ preprint (no network call).
    Returns True only when the newest version's PDF is on disk with
    >= TEXT_FLOOR chars of text and meta.json is written.
    Returns False on any error or when neither server knows the DOI, so the
    ladder falls through.
    """
    if not _PREFIX.match(doi):
        return None

    dest_pdf = pdf_path(doi)
    dest_txt = text_path(doi)
    dest_meta = meta_path(doi)

    def _cleanup() -> None:
        for p in (dest_pdf, dest_txt, dest_meta):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    try:
        found = _lookup(doi, mailto)
        if found is None:
            return False
        record, server = found

        version = _parse_version(record.get("version"))
        if version is None:
            return False

        pdf_url = PDF_URL.format(server=server, doi=urllib.parse.quote(doi), version=version)
        # Both hosts sit behind Cloudflare and answer 403 intermittently for
        # the same URL. One retry after a short gap roughly doubles the hit rate.
        try:
            download_pdf(pdf_url, dest_pdf, mailto)
        except FetchError:
            time.sleep(RETRY_GAP_SEC)
            download_pdf(pdf_url, dest_pdf, mailto)
        n = write_text(dest_pdf, dest_txt)
        if n < TEXT_FLOOR:
            _cleanup()
            return False

        published = str(record.get("published") or "").strip()
        write_meta(
            doi,
            {
                "title": str(record.get("title") or "").strip(),
                "resolver": "biorxiv",
                "version": f"v{version}",
                "server": server,
                "license": str(record.get("license") or "").strip() or None,
                "journal": "bioRxiv" if server == "biorxiv" else "medRxiv",
                "year": _parse_year(record.get("date")),
                "text_chars": n,
                "published_doi": published if published and published.upper() != "NA" else None,
            },
        )
        return True
    except Exception:
        _cleanup()
        return False


def _lookup(doi: str, mailto: str) -> tuple[dict, str] | None:
    """Ask bioRxiv first, then medRxiv. Return (newest record, server) or None.

    The wrong server answers HTTP 200 with an empty collection, so an empty
    or unreadable answer on one server means "try the other", not "give up".
    """
    for endpoint in SERVERS:
        url = API_URL.format(server=endpoint, doi=urllib.parse.quote(doi))
        try:
            data = json.loads(fetch_bytes(url, mailto).decode("utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        records = [r for r in data.get("collection") or [] if isinstance(r, dict)]
        if not records:
            continue
        record = max(records, key=lambda r: _parse_version(r.get("version")) or 0)
        server = str(record.get("server") or "").strip().lower()
        if server not in SERVERS:
            server = endpoint
        return record, server
    return None


def _parse_version(raw: object) -> int | None:
    try:
        return int(str(raw).strip().lstrip("vV"))
    except (TypeError, ValueError):
        return None


def _parse_year(raw: object) -> int | None:
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    return None
