"""OpenAlex resolver."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from papers.cache import (
    TEXT_FLOOR,
    meta_path,
    pdf_path,
    text_path,
    write_meta,
)
from papers.extract import write_text
from papers.fetch import FetchError, TIMEOUT_SEC, download_pdf, user_agent


def _is_paid_cdn(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return host == "content.openalex.org" or host.endswith(".content.openalex.org")
    except Exception:
        return False


def resolve(doi: str, mailto: str) -> str:
    """Resolve a DOI via OpenAlex. Returns 'hit', 'miss', or 'unreadable'."""
    quoted_doi = urllib.parse.quote(doi, safe="")
    params = urllib.parse.urlencode({"mailto": mailto})
    url = f"https://api.openalex.org/works/https://doi.org/{quoted_doi}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(mailto)},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "miss"

    if not isinstance(data, dict):
        return "miss"

    loc = data.get("best_oa_location")
    if not isinstance(loc, dict):
        return "miss"

    pdf_url = loc.get("pdf_url")
    if not pdf_url or not isinstance(pdf_url, str):
        return "miss"

    if _is_paid_cdn(pdf_url):
        return "miss"

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
        download_pdf(pdf_url, dest_pdf, mailto)
    except Exception:
        _cleanup()
        return "miss"

    try:
        n = write_text(dest_pdf, dest_txt)
        title = data.get("display_name") or data.get("title") or ""
        prim_loc = data.get("primary_location")
        prim_source = prim_loc.get("source") if isinstance(prim_loc, dict) else {}
        journal = prim_source.get("display_name") if isinstance(prim_source, dict) else None
        year = data.get("publication_year")

        write_meta(
            doi,
            {
                "title": title,
                "resolver": "openalex",
                "version": loc.get("version"),
                "license": loc.get("license"),
                "journal": journal,
                "year": year,
                "text_chars": n,
            },
        )
        if n >= TEXT_FLOOR:
            return "hit"
        return "unreadable"
    except Exception:
        _cleanup()
        return "miss"
