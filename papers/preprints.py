"""Preprint shortcut resolver for bioRxiv, medRxiv, OSF, arXiv, and others."""

from __future__ import annotations

import re
import time

from papers.cache import (
    TEXT_FLOOR,
    pdf_path,
    text_path,
    write_meta,
)
from papers.extract import write_text
from papers.fetch import FetchError, download_pdf
from papers.landing import download_from_landing

_last_arxiv_request: float = 0.0
ARXIV_GAP_SEC: float = 3.0
# bioRxiv/medRxiv sit behind Cloudflare and answer 403 intermittently for the
# same URL; one retry roughly doubles the hit rate.
RETRY_GAP_SEC: float = 2.0
# These hosts 403 the script UA; the constructed URL still belongs in
# browser_urls so a real browser can try it.
_BLOCK_PREFIXES = ("10.26434/chemrxiv", "10.20944/preprints")


def _rate_limit_arxiv() -> None:
    global _last_arxiv_request
    now = time.time()
    elapsed = now - _last_arxiv_request
    if _last_arxiv_request > 0 and elapsed < ARXIV_GAP_SEC:
        time.sleep(ARXIV_GAP_SEC - elapsed)
    _last_arxiv_request = time.time()


def _is_http_403(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "code", None) == 403:
            return True
        if "HTTP Error 403" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _targets(doi: str) -> tuple[list[str], bool] | None:
    """(urls, is_arxiv) if the DOI matches a preprint host, else None."""
    if re.match(r"^10\.1101/\d", doi):
        return [
            f"https://www.biorxiv.org/content/{doi}v1.full.pdf",
            f"https://www.medrxiv.org/content/{doi}v1.full.pdf",
        ], False
    if re.match(r"^10\.312(19|22|24|34|35|36)/osf\.io/", doi) or doi.startswith(
        "10.35542/osf.io/"
    ):
        id_ = doi.split("/osf.io/", 1)[-1].strip()
        return ([f"https://osf.io/{id_}/download"] if id_ else []), False
    if doi.startswith("10.48550/arxiv."):
        id_ = doi[len("10.48550/arxiv.") :].strip()
        return ([f"https://arxiv.org/pdf/{id_}.pdf"] if id_ else []), True
    if doi.startswith("10.21203/"):
        rest = doi[len("10.21203/") :].strip()
        m = re.search(r"(rs-\d+)(?:/v(\d+))?", rest)
        if not m:
            return [], False
        ver = m.group(2) or "1"
        return [f"https://www.researchsquare.com/article/{m.group(1)}/v{ver}.pdf"], False
    if doi.startswith("10.26434/chemrxiv"):
        return [f"https://doi.org/{doi}"], False
    if doi.startswith("10.20944/preprints"):
        rest = doi[len("10.20944/preprints") :].strip()
        m = re.match(r"(\d{6}\.\d+)(?:\.v(\d+))?", rest)
        if not m:
            return [], False
        ver = m.group(2) or "1"
        return [
            f"https://www.preprints.org/manuscript/{m.group(1)}/v{ver}/download"
        ], False
    return None


def shortcut_urls(doi: str) -> list[str]:
    """Reconstructable PDF URLs for a matching preprint DOI, else []."""
    got = _targets(doi)
    return list(got[0]) if got else []


def resolve(doi: str, mailto: str) -> str | None:
    """Resolve preprint DOIs directly.

    Returns:
        'hit' - PDF downloaded and text >= TEXT_FLOOR
        'unreadable' - PDF downloaded but text < TEXT_FLOOR
        'blocked' - ChemRxiv / preprints.org, every URL HTTP 403
        'miss' - prefix matched but downloads failed (or empty identifier)
        None - DOI prefix did not match any preprint server
    """
    got = _targets(doi)
    if got is None:
        return None
    urls, is_arxiv = got
    if not urls:
        return "miss"

    dest_pdf = pdf_path(doi)
    dest_txt = text_path(doi)

    # One pass over every host first (bioRxiv then medRxiv). Retrying a 403
    # on the wrong host before trying the other one burns Cloudflare budget
    # and gets the right host blocked too.
    result = _try_urls(doi, urls, dest_pdf, dest_txt, mailto, is_arxiv)
    if result in ("hit", "unreadable"):
        return result
    time.sleep(RETRY_GAP_SEC)
    result = _try_urls(doi, urls, dest_pdf, dest_txt, mailto, is_arxiv)
    if result in ("hit", "unreadable"):
        return result
    if result == "blocked" and doi.startswith(_BLOCK_PREFIXES):
        return "blocked"
    return "miss"


def _drop(path) -> None:
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _try_urls(
    doi: str,
    urls: list[str],
    dest_pdf,
    dest_txt,
    mailto: str,
    is_arxiv: bool,
) -> str | None:
    """Try each URL once. Return 'hit'/'unreadable'/'blocked', or None."""
    all_403 = True
    for url in urls:
        if is_arxiv:
            _rate_limit_arxiv()
        try:
            download_pdf(url, dest_pdf, mailto)
        except FetchError as exc:
            url_403 = _is_http_403(exc)
            if is_arxiv or not download_from_landing(url, dest_pdf, mailto):
                _drop(dest_pdf)
                if not url_403:
                    all_403 = False
                continue
        except Exception:
            _drop(dest_pdf)
            all_403 = False
            continue

        try:
            n = write_text(dest_pdf, dest_txt)
            write_meta(
                doi,
                {
                    "title": "",
                    "resolver": "preprint",
                    "version": "preprint",
                    "license": None,
                    "text_chars": n,
                },
            )
            if n >= TEXT_FLOOR:
                return "hit"
            return "unreadable"
        except Exception:
            _drop(dest_pdf)
            all_403 = False
            continue
    return "blocked" if all_403 else None
