from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from papers.cache import TEXT_FLOOR

_FULLTEXT_SKIP_TITLES = re.compile(
    r"acknowledg|funding|reference|supplementar|abbreviation|author contribution"
    r"|conflict|competing|availability|ethics|consent|orcid|appendix"
)


def _jats_text(node: ET.Element) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def extract_jats(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return ""
    body = root.find(".//body")
    if body is None:
        return ""
    chunks: list[str] = []
    for sec in body.findall("sec"):
        title_node = sec.find("title")
        title = _jats_text(title_node) if title_node is not None else ""
        if title and _FULLTEXT_SKIP_TITLES.search(title.lower()):
            continue
        text = _jats_text(sec)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks) if chunks else _jats_text(body)


def write_jats_text(xml_text: str, dest: Path) -> int:
    text = extract_jats(xml_text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return len(text.strip())


_HTML_KEEP_CLASSES = ("abstract", "main-article-body")
_HTML_SKIP_CLASSES = ("ref-list", "ack", "kwd-group")
_HTML_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer"})
_HTML_BLOCK_TAGS = frozenset({"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "li", "tr", "table", "caption"})


class _PmcArticleParser(HTMLParser):
    """Collect the abstract and body text of a PMC article page.

    PMC wraps the article in ``<section class="abstract">`` and
    ``<section class="body main-article-body">``; everything else on the page
    (banner, search box, side navigation, footer) is chrome. References,
    acknowledgements and keyword lists are dropped inside the body, matching
    what ``extract_jats`` skips.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, opens_keep, opens_skip)
        self._keep = 0
        self._skip = 0
        self.chunks: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        opens_keep = any(c in classes for c in _HTML_KEEP_CLASSES)
        opens_skip = tag in _HTML_SKIP_TAGS or any(c in classes for c in _HTML_SKIP_CLASSES)
        if opens_keep:
            self._keep += 1
        if opens_skip:
            self._skip += 1
        self._stack.append((tag, opens_keep, opens_skip))
        if tag in _HTML_BLOCK_TAGS:
            self.chunks.append("\n")
        elif tag in ("td", "th"):
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        # Pop back to the matching open tag; HTML in the wild leaves tags unclosed.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, opens_keep, opens_skip in self._stack[i:]:
                    if opens_keep:
                        self._keep -= 1
                    if opens_skip:
                        self._skip -= 1
                del self._stack[i:]
                break
        if tag in _HTML_BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._keep > 0 and self._skip == 0:
            self.chunks.append(data)


def extract_html(html_text: str) -> str:
    """Text of a PMC article page (``pmc.ncbi.nlm.nih.gov/articles/PMCxxx/``).

    Returns "" when the page does not carry PMC's article containers, so a
    login wall, an error page or some other site never masquerades as text.
    """
    parser = _PmcArticleParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return ""
    raw = "".join(parser.chunks)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF")


def extract_text(pdf: Path) -> str:
    try:
        reader = PdfReader(str(pdf))
    except (PdfReadError, OSError, ValueError):
        return ""
    parts: list[str] = []
    try:
        for page in reader.pages:
            parts.append(page.extract_text() or "")
    except Exception:
        return "".join(parts)
    return "\n".join(parts)


def write_text(pdf: Path, dest: Path) -> int:
    text = extract_text(pdf)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return len(text.strip())


def is_readable(n_chars: int) -> bool:
    return n_chars >= TEXT_FLOOR
