"""Semantic Scholar resolver."""

from __future__ import annotations

import json
import os
import time
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

_sleep = time.sleep
_skip_for_process = False


def resolve(doi: str, mailto: str) -> str | None:
    """Resolve a DOI via Semantic Scholar. Returns 'hit', 'miss', 'unreadable', or None."""
    global _skip_for_process
    if _skip_for_process:
        return None

    quoted_doi = urllib.parse.quote(doi, safe="/")  # S2 answers 429 to %2F-encoded DOIs
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quoted_doi}"
        "?fields=title,openAccessPdf,externalIds"
    )
    headers = {"User-Agent": user_agent(mailto)}
    api_key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(url, headers=headers)
    data = None

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _sleep(30)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as retry_exc:
                if retry_exc.code == 429:
                    _skip_for_process = True
                    return None
                return "miss"
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
                return "miss"
        else:
            return "miss"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "miss"

    if not isinstance(data, dict):
        return "miss"

    oa_pdf = data.get("openAccessPdf")
    pdf_url = None
    license_val = None
    if isinstance(oa_pdf, dict):
        status = str(oa_pdf.get("status") or "").strip().upper()
        raw_url = oa_pdf.get("url")
        if status != "CLOSED" and isinstance(raw_url, str) and raw_url.startswith("http"):
            pdf_url = raw_url
            license_val = oa_pdf.get("license") or None

    if not pdf_url:
        ext = data.get("externalIds") if isinstance(data.get("externalIds"), dict) else {}
        arxiv_id = ext.get("ArXiv") if isinstance(ext, dict) else None
        if isinstance(arxiv_id, str) and arxiv_id.strip():
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id.strip()}.pdf"

    if not pdf_url:
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
        raw_title = data.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        write_meta(
            doi,
            {
                "title": title,
                "resolver": "semanticscholar",
                "version": None,
                "license": license_val,
                "text_chars": n,
            },
        )
        if n >= TEXT_FLOOR:
            return "hit"
        return "unreadable"
    except Exception:
        _cleanup()
        return "miss"
