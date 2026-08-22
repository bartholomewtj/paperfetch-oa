"""CORE resolver (https://core.ac.uk) — repositories Unpaywall misses.

Only runs when CORE_API_KEY is set: keyless calls are capped at 10 per
10 minutes, which is not enough to be useful.
"""

from __future__ import annotations

import json
import os
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
from papers.fetch import TIMEOUT_SEC, download_pdf, user_agent

# search/works, not /discover: discover is POST-only and its backend 500s
# (checked 18 Aug 2026). Trailing slash avoids a 301.
SEARCH_URL = "https://api.core.ac.uk/v3/search/works/?"
MAX_RESULTS = 5


def _candidates(data: object, doi: str) -> tuple[list[str], str]:
    """Download URLs for the result whose DOI matches, plus its title.

    CORE-hosted downloadUrl first, then the repository's own
    sourceFulltextUrls. Live check 18 Aug 2026: the CORE fileserver
    answered 400 "No repository ID" on every record tried, while the
    repository copies worked, so the caller must try each in turn.
    CORE also carries records with the wrong DOI attached (seen on
    10.1136/bmj.n71); those had no links, and the %PDF gate catches
    anything else.
    """
    if not isinstance(data, dict):
        return [], ""
    results = data.get("results")
    if not isinstance(results, list):
        return [], ""
    want = doi.lower()
    for r in results:
        if not isinstance(r, dict):
            continue
        if (r.get("doi") or "").lower() != want:
            continue
        cands = [r.get("downloadUrl")]
        src = r.get("sourceFulltextUrls")
        if isinstance(src, list):
            cands.extend(src)
        urls = [u for u in cands if u and isinstance(u, str) and u.startswith("http")]
        if urls:
            title = r.get("title")
            return urls, title if isinstance(title, str) else ""
    return [], ""


def resolve(doi: str, mailto: str) -> str | None:
    """Return 'hit', 'miss', 'unreadable', or None when CORE_API_KEY is unset."""
    api_key = (os.environ.get("CORE_API_KEY") or "").strip()
    if not api_key:
        return None

    url = SEARCH_URL + urllib.parse.urlencode({"q": f'doi:"{doi}"', "limit": MAX_RESULTS})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent(mailto),
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "miss"

    urls, title = _candidates(data, doi)
    if not urls:
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

    for pdf_url in urls:
        try:
            download_pdf(pdf_url, dest_pdf, mailto)
        except Exception:
            _cleanup()
            continue
        try:
            n = write_text(dest_pdf, dest_txt)
            write_meta(
                doi,
                {
                    "title": title,
                    "resolver": "core",
                    "version": None,
                    "license": None,
                    "text_chars": n,
                },
            )
            return "hit" if n >= TEXT_FLOOR else "unreadable"
        except Exception:
            _cleanup()
            continue
    return "miss"
