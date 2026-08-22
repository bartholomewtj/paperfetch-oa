"""Preprint shortcut resolver for bioRxiv, medRxiv, PsyArXiv, and arXiv."""

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

_last_arxiv_request: float = 0.0
ARXIV_GAP_SEC: float = 3.0
# bioRxiv/medRxiv sit behind Cloudflare and answer 403 intermittently for the
# same URL; one retry roughly doubles the hit rate.
RETRY_GAP_SEC: float = 2.0


def _rate_limit_arxiv() -> None:
    global _last_arxiv_request
    now = time.time()
    elapsed = now - _last_arxiv_request
    if _last_arxiv_request > 0 and elapsed < ARXIV_GAP_SEC:
        time.sleep(ARXIV_GAP_SEC - elapsed)
    _last_arxiv_request = time.time()


def resolve(doi: str, mailto: str) -> str | None:
    """Resolve preprint DOIs directly.

    Returns:
        'hit' - PDF downloaded and text >= TEXT_FLOOR
        'unreadable' - PDF downloaded but text < TEXT_FLOOR
        'miss' - prefix matched but downloads failed (or empty identifier)
        None - DOI prefix did not match any preprint server
    """
    matched = False
    urls: list[str] = []
    is_arxiv = False

    if re.match(r"^10\.1101/\d", doi):
        matched = True
        urls = [
            f"https://www.biorxiv.org/content/{doi}v1.full.pdf",
            f"https://www.medrxiv.org/content/{doi}v1.full.pdf",
        ]
    elif doi.startswith("10.31234/osf.io/"):
        matched = True
        id_ = doi[len("10.31234/osf.io/") :].strip()
        if id_:
            urls = [f"https://osf.io/{id_}/download"]
    elif doi.startswith("10.48550/arxiv."):
        matched = True
        id_ = doi[len("10.48550/arxiv.") :].strip()
        if id_:
            is_arxiv = True
            urls = [f"https://arxiv.org/pdf/{id_}.pdf"]

    if not matched:
        return None

    if not urls:
        return "miss"

    dest_pdf = pdf_path(doi)
    dest_txt = text_path(doi)

    for url in urls:
        if is_arxiv:
            _rate_limit_arxiv()
        try:
            try:
                download_pdf(url, dest_pdf, mailto)
            except FetchError:
                time.sleep(RETRY_GAP_SEC)
                download_pdf(url, dest_pdf, mailto)
        except FetchError:
            if dest_pdf.exists():
                try:
                    dest_pdf.unlink()
                except OSError:
                    pass
            continue
        except Exception:
            if dest_pdf.exists():
                try:
                    dest_pdf.unlink()
                except OSError:
                    pass
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
            if dest_pdf.exists():
                try:
                    dest_pdf.unlink()
                except OSError:
                    pass
            continue

    return "miss"
