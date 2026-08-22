"""Crossref title-to-DOI resolver."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from papers.cache import normalize_doi
from papers.fetch import TIMEOUT_SEC, user_agent


# Crossref ranks reviewer reports and "Faculty Opinions" datasets above the
# article they describe. Skip those record types.
SKIP_TYPES = {"peer-review", "dataset", "component", "grant"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _pick_doi(items: list, query: str) -> str | None:
    """First usable item; an exact (normalised) title match wins."""
    q = _norm(query)
    usable = []
    for it in items:
        if not isinstance(it, dict) or it.get("type") in SKIP_TYPES:
            continue
        doi = it.get("DOI")
        if not doi or not isinstance(doi, str):
            continue
        titles = it.get("title") or []
        if any(_norm(x) == q for x in titles if isinstance(x, str)):
            return doi
        usable.append(doi)
    return usable[0] if usable else None


def resolve_title(title: str, mailto: str) -> str | None:
    """GET Crossref works?query.bibliographic=&rows=1&mailto=. Return normalised DOI or None."""
    try:
        t = (title or "").strip()
        if not t:
            return None
        params = urllib.parse.urlencode(
            {
                "query.bibliographic": t,
                "rows": 5,
                "mailto": mailto,
            }
        )
        url = f"https://api.crossref.org/works?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent(mailto)},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not isinstance(data, dict):
            return None
        msg = data.get("message")
        if not isinstance(msg, dict):
            return None
        items = msg.get("items")
        if not isinstance(items, list) or not items:
            return None
        raw_doi = _pick_doi(items, t)
        if not raw_doi:
            return None
        return normalize_doi(raw_doi)
    except Exception:
        return None
