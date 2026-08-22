"""Crossref title-to-DOI resolver."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from papers.cache import normalize_doi
from papers.fetch import TIMEOUT_SEC, user_agent


# Crossref ranks reviewer reports, "Faculty Opinions" datasets, and
# posted-content (preprint servers) above the journal article they
# describe. Nature Precedings is typed as journal-article and ranks
# above PLOS for the same title — skip it by DOI prefix / container.
SKIP_TYPES = {"peer-review", "dataset", "component", "grant", "posted-content"}
SKIP_DOI_PREFIXES = ("10.1038/npre.",)
SKIP_CONTAINERS = {"nature precedings"}
ROWS = 10


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _container(it: dict) -> str:
    raw = it.get("container-title") or []
    if isinstance(raw, list) and raw:
        return _norm(str(raw[0]))
    if isinstance(raw, str):
        return _norm(raw)
    return ""


def _is_weak(it: dict) -> bool:
    doi = (it.get("DOI") or "").lower()
    if any(doi.startswith(p) for p in SKIP_DOI_PREFIXES):
        return True
    return _container(it) in SKIP_CONTAINERS


def _rank(it: dict) -> tuple[int, int, int]:
    """Higher is better. Weak records (Nature Precedings) lose to a journal."""
    if _is_weak(it):
        return (0, 0, 0)
    is_article = 1 if it.get("type") == "journal-article" else 0
    has_issn = 1 if (it.get("ISSN") or it.get("issn-type")) else 0
    return (1, is_article, has_issn)


def _pick_doi(items: list, query: str) -> str | None:
    """Exact (normalised) title match wins; otherwise a journal-article.

    Several Crossref hits can share one title (a preprint plus the journal
    version). Rank those instead of taking the first.
    """
    q = _norm(query)
    exact: list[dict] = []
    journal: list[dict] = []
    other: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or it.get("type") in SKIP_TYPES:
            continue
        doi = it.get("DOI")
        if not doi or not isinstance(doi, str):
            continue
        titles = it.get("title") or []
        if any(_norm(x) == q for x in titles if isinstance(x, str)):
            exact.append(it)
        elif it.get("type") == "journal-article":
            journal.append(it)
        else:
            other.append(it)
    pool = exact or journal or other
    if not pool:
        return None
    return max(pool, key=_rank).get("DOI")


def resolve_title(title: str, mailto: str) -> str | None:
    """GET Crossref works?query.bibliographic=&rows=10&mailto=. Return normalised DOI or None."""
    try:
        t = (title or "").strip()
        if not t:
            return None
        params = urllib.parse.urlencode(
            {
                "query.bibliographic": t,
                "rows": ROWS,
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
