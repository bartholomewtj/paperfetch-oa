"""OpenAIRE Graph API resolver — instance URLs Unpaywall often lacks."""

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
from papers.fetch import TIMEOUT_SEC, FetchError, download_pdf, user_agent
from papers.landing import download_from_landing, is_publisher_url, looks_like_pdf_url

SEARCH_URL = "https://api.openaire.eu/graph/v3/research-products"


def _instance_urls(data: object, doi: str) -> tuple[list[str], str]:
    if not isinstance(data, dict):
        return [], ""
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return [], ""
    item = results[0]
    if not isinstance(item, dict):
        return [], ""
    title = item.get("mainTitle") or item.get("title") or ""
    title = title if isinstance(title, str) else ""
    want = doi.lower()
    urls: list[str] = []
    seen: set[str] = set()
    for inst in item.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        rights = inst.get("accessRight") or {}
        label = str(rights.get("label") or "").strip().upper()
        if label == "CLOSED":
            continue
        for raw in inst.get("urls") or []:
            if not isinstance(raw, str) or not raw.startswith("http"):
                continue
            url = raw.strip()
            if url in seen or is_publisher_url(url):
                continue
            host = urllib.parse.urlparse(url).hostname or ""
            if host.endswith("doi.org") or host.endswith("pubmed.ncbi.nlm.nih.gov"):
                continue
            pids = inst.get("pids") or []
            if isinstance(pids, list):
                inst_doi = ""
                for p in pids:
                    if isinstance(p, dict) and str(p.get("scheme") or "").lower() == "doi":
                        inst_doi = str(p.get("value") or "").lower()
                if inst_doi and inst_doi != want:
                    continue
            seen.add(url)
            urls.append(url)
    return urls, title


def resolve(doi: str, mailto: str) -> str | None:
    """Return 'hit', 'miss', or 'unreadable'."""
    params = urllib.parse.urlencode({"pid": doi, "pageSize": "1"})
    url = f"{SEARCH_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(mailto), "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "miss"

    urls, title = _instance_urls(data, doi)
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

    last_unreadable = False
    for pdf_url in urls:
        try:
            if looks_like_pdf_url(pdf_url):
                download_pdf(pdf_url, dest_pdf, mailto)
            elif not download_from_landing(pdf_url, dest_pdf, mailto):
                _cleanup()
                continue
        except (FetchError, OSError, TimeoutError):
            _cleanup()
            continue
        except Exception:
            _cleanup()
            continue
        try:
            n = write_text(dest_pdf, dest_txt)
            write_meta(
                doi,
                {
                    "title": title,
                    "resolver": "openaire",
                    "version": None,
                    "license": None,
                    "text_chars": n,
                },
            )
            if n >= TEXT_FLOOR:
                return "hit"
            last_unreadable = True
            _cleanup()
        except Exception:
            _cleanup()
            continue
    return "unreadable" if last_unreadable else "miss"
