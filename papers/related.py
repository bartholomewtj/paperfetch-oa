"""Journal DOI → preprint DOI via Crossref has-preprint, then fetch that copy."""

from __future__ import annotations

import shutil

from papers.biorxiv import resolve as biorxiv_resolve
from papers.cache import normalize_doi, paper_dir
from papers.crossref import preprint_of
from papers.preprints import resolve as preprint_resolve


def _copy_cache(src_doi: str, dest_doi: str) -> None:
    src = paper_dir(src_doi)
    dest = paper_dir(dest_doi)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("paper.pdf", "text.txt", "meta.json"):
        s = src / name
        if s.exists():
            shutil.copy2(s, dest / name)


def resolve(doi: str, mailto: str) -> str | None:
    """Return 'hit', 'miss', 'unreadable', or None when Crossref has no preprint."""
    other = preprint_of(doi, mailto)
    if not other:
        return None
    other = normalize_doi(other)
    if other == normalize_doi(doi):
        return None

    br = biorxiv_resolve(other, mailto)
    if br is True:
        _copy_cache(other, doi)
        return "hit"

    pr = preprint_resolve(other, mailto)
    if pr in ("hit", True):
        _copy_cache(other, doi)
        return "hit"
    if pr == "unreadable":
        _copy_cache(other, doi)
        return "unreadable"
    if pr in ("miss", False):
        return "miss"
    return "miss"
