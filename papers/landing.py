"""Follow a repository landing page to a PDF. One hop, SSRF-gated."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from papers.extract import looks_like_pdf
from papers.fetch import FetchError, UnsafeUrl, download_pdf, fetch_bytes

MAX_HTML_BYTES = 2 * 1024 * 1024

_PUBLISHER_HOST = re.compile(
    r"(^|\.)("
    r"nature\.com|cell\.com|sciencedirect\.com|elsevier\.com|wiley\.com|"
    r"springer(?:nature)?\.com|oup\.com|academic\.oup\.com|nejm\.org|"
    r"jamanetwork\.com|lww\.com|wolterskluwer\.com|informs\.org|"
    r"sagepub\.com|aaas\.org|science\.org|thelancet\.com|"
    r"bmj\.com|karger\.com|thieme(?:-connect)?\.com"
    r")$",
    re.IGNORECASE,
)

_PDF_HREF = re.compile(
    r"\.pdf($|\?)|/bitstream/|/download(/|$)|/pdf/|/full\.pdf",
    re.IGNORECASE,
)


def is_publisher_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return bool(host and _PUBLISHER_HOST.search(host))


def looks_like_pdf_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return bool(_PDF_HREF.search(path) or path.endswith(".pdf"))


class _PdfLinkParser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.urls: list[str] = []
        self._seen: set[str] = set()

    def _add(self, raw: str | None) -> None:
        if not raw or not isinstance(raw, str):
            return
        url = urljoin(self.base, raw.strip())
        if not url.startswith("http"):
            return
        if url in self._seen or is_publisher_url(url):
            return
        self._seen.add(url)
        self.urls.append(url)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k.lower(): v for k, v in attrs}
        if tag == "meta" and (d.get("name") or "").lower() == "citation_pdf_url":
            self._add(d.get("content"))
            return
        if tag == "link":
            typ = (d.get("type") or "").lower()
            rel = (d.get("rel") or "").lower()
            if "pdf" in typ or "alternate" in rel:
                href = d.get("href") or ""
                if "pdf" in typ or looks_like_pdf_url(href):
                    self._add(href)
            return
        if tag == "a":
            href = d.get("href") or ""
            if looks_like_pdf_url(href):
                self._add(href)


def pdf_urls_from_html(html: str, base: str) -> list[str]:
    parser = _PdfLinkParser(base)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return parser.urls


def download_from_landing(url: str, dest, mailto: str) -> bool:
    """Fetch `url`. If it is a PDF, write it; else parse HTML for a PDF link.

    One hop. Returns True when dest is a PDF. Publisher hosts are skipped.
    """
    if not url or not url.startswith("http") or is_publisher_url(url):
        return False
    try:
        data = fetch_bytes(url, mailto)
    except (FetchError, UnsafeUrl, OSError, TimeoutError):
        return False
    if looks_like_pdf(data):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    if len(data) > MAX_HTML_BYTES:
        return False
    html = data.decode("utf-8", errors="replace")
    for pdf_url in pdf_urls_from_html(html, url):
        try:
            download_pdf(pdf_url, dest, mailto)
            return True
        except (FetchError, UnsafeUrl, OSError, TimeoutError):
            continue
        except Exception:
            continue
    return False
