"""Full text for the cache: PDF (PyMuPDF), JATS XML (Europe PMC) and PMC HTML.

Every route writes the same shape of text.txt: a marker line ``## Results``
before each recognised section, a blank line after it, and the reference
list dropped. Section titles the routes do not recognise stay in the body
with no marker, under whichever recognised section came before them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pymupdf

from papers.cache import TEXT_FLOOR

# Sections dropped on every route: back matter that adds nothing for a reader
# and whose volume/page numbers can masquerade as a result being checked.
_FULLTEXT_SKIP_TITLES = re.compile(
    r"acknowledg|funding|reference|bibliograph|literature cited|supplementar"
    r"|supporting info|abbreviation|author contribution|conflict|competing"
    r"|availability|ethics|consent|orcid|appendix"
)

# Heading text -> canonical name. Matched against the whole heading after
# numbering ("2.", "III", "2 |") and trailing punctuation are stripped.
_CANONICAL = [
    ("abstract", re.compile(r"(structured )?abstract")),
    ("introduction", re.compile(r"introduction|background( and (objectives?|aims?))?")),
    (
        "methods",
        re.compile(
            r"((materials?|patients?|subjects?|participants?|study design|experimental|"
            r"design)( and | & |, ))?(methods?|methodology|procedures?)( and materials?)?"
            r"|experimental (procedures?|section)|study design and methods?"
        ),
    ),
    ("results", re.compile(r"results?( and discussion)?|findings|main results")),
    ("discussion", re.compile(r"discussion")),
    ("conclusions", re.compile(r"(summary and )?conclusions?|concluding remarks")),
    (
        "references",
        re.compile(r"references?( and notes| list| cited)?|bibliography|literature cited|works cited"),
    ),
]
_HEADING_PREFIX = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*|[ivx]+)[.):|\s]+)?", re.IGNORECASE)

_JATS_SEC_TYPES = {
    "intro": "introduction",
    "introduction": "introduction",
    "materials": "methods",
    "methods": "methods",
    "materials|methods": "methods",
    "results": "results",
    "discussion": "discussion",
    "conclusions": "conclusions",
    "conclusion": "conclusions",
}


def canonical_section(title: str) -> str | None:
    """'2. MATERIALS AND METHODS' -> 'methods'; None when not a standard heading."""
    t = _HEADING_PREFIX.sub("", (title or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip(" .:;-")
    if not t:
        return None
    for name, pattern in _CANONICAL:
        if pattern.fullmatch(t):
            return name
    return None


def is_skipped_section(title: str) -> bool:
    return bool(_FULLTEXT_SKIP_TITLES.search((title or "").lower()))


def _assemble(segments: list[tuple[str | None, str]]) -> str:
    """[(canonical name or None, body text)] -> text.txt with marker lines."""
    parts: list[str] = []
    for name, body in segments:
        body = body.strip()
        if name is None:
            if body:
                parts.append(body)
            continue
        parts.append(f"## {name.title()}\n\n{body}" if body else f"## {name.title()}\n")
    return "\n\n".join(parts).strip() + ("\n" if parts else "")


# ---------------------------------------------------------------- JATS XML


def _jats_text(node: ET.Element) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _jats_body_text(sec: ET.Element, title_node: ET.Element | None) -> str:
    """Section text without its own title (the marker line carries that)."""
    if title_node is None:
        return _jats_text(sec)
    pieces = [title_node.tail or ""]
    for child in sec:
        if child is title_node:
            continue
        pieces.append(" ".join(child.itertext()))
        pieces.append(child.tail or "")
    text = (sec.text or "") + " ".join(pieces)
    return re.sub(r"\s+", " ", text).strip()


def extract_jats(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return ""
    body = root.find(".//body")
    if body is None:
        return ""
    segments: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    abstract = root.find(".//front//abstract")
    if abstract is not None:
        text = _jats_text(abstract)
        text = re.sub(r"^abstract\s*", "", text, flags=re.IGNORECASE)
        if text:
            segments.append(("abstract", text))
            seen.add("abstract")
    for sec in body.findall("sec"):
        title_node = sec.find("title")
        title = _jats_text(title_node) if title_node is not None else ""
        if title and is_skipped_section(title):
            continue
        name = canonical_section(title) or _JATS_SEC_TYPES.get((sec.get("sec-type") or "").lower())
        if name == "references":
            continue
        if name and name not in seen:
            seen.add(name)
            text = _jats_body_text(sec, title_node)
        else:
            name = None
            text = _jats_text(sec)
        if text:
            segments.append((name, text))
    if not any(name for name, _ in segments):
        plain = "\n\n".join(body for _, body in segments)
        return plain if plain else _jats_text(body)
    return _assemble(segments)


def write_jats_text(xml_text: str, dest: Path) -> int:
    text = extract_jats(xml_text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return len(text.strip())


# ---------------------------------------------------------------- PMC HTML

_HTML_KEEP_CLASSES = ("abstract", "main-article-body")
_HTML_SKIP_CLASSES = ("ref-list", "ack", "kwd-group")
_HTML_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer"})
_HTML_BLOCK_TAGS = frozenset({"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "li", "tr", "table", "caption"})
_HTML_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
_SENTINEL = "\x00"


class _PmcArticleParser(HTMLParser):
    """Collect the abstract and body text of a PMC article page.

    PMC wraps the article in ``<section class="abstract">`` and
    ``<section class="body main-article-body">``; everything else on the page
    (banner, search box, side navigation, footer) is chrome. References,
    acknowledgements and keyword lists are dropped inside the body, matching
    what ``extract_jats`` skips. Headings that name a standard section become
    marker sentinels; other headings stay as plain lines.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, opens_keep, opens_skip)
        self._keep = 0
        self._skip = 0
        self._heading: list[str] | None = None
        self.seen: set[str] = set()
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
        if tag in _HTML_HEADING_TAGS and self._keep > 0 and self._skip == 0:
            self._heading = []
        if tag in _HTML_BLOCK_TAGS:
            self.chunks.append("\n")
        elif tag in ("td", "th"):
            self.chunks.append(" ")

    def _close_heading(self) -> None:
        if self._heading is None:
            return
        title = re.sub(r"\s+", " ", "".join(self._heading)).strip()
        self._heading = None
        name = canonical_section(title)
        if name and name != "references" and name not in self.seen:
            self.seen.add(name)
            self.chunks.append(f"\n{_SENTINEL}{name}\n")
        elif title:
            self.chunks.append(title)

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_HEADING_TAGS:
            self._close_heading()
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
        if self._heading is not None:
            self._heading.append(data)
        elif self._keep > 0 and self._skip == 0:
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
    segments: list[tuple[str | None, list[str]]] = [(None, [])]
    for ln in lines:
        if not ln:
            continue
        if ln.startswith(_SENTINEL):
            segments.append((ln[1:], []))
        else:
            segments[-1][1].append(ln)
    if len(segments) == 1:
        return "\n".join(segments[0][1])
    return _assemble([(name, "\n".join(body)) for name, body in segments])


# ---------------------------------------------------------------- PDF


def looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF")


_MARGIN = 0.07  # top and bottom 7% of the page hold running headers and footers


def _pdf_lines(doc) -> list[dict]:
    """One record per text line, in reading order, with font size and margin flag."""
    lines: list[dict] = []
    for pno, page in enumerate(doc):
        height = page.rect.height or 1.0
        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            continue
        for block in blocks:
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in line["spans"]).strip()
                y0, y1 = line["bbox"][1], line["bbox"][3]
                lines.append(
                    {
                        "page": pno,
                        "text": text,
                        "size": max(s["size"] for s in spans),
                        "bold": all(("Bold" in s.get("font", "")) or (s.get("flags", 0) & 16) for s in spans),
                        "first": spans[0],
                        "margin": y1 < height * _MARGIN or y0 > height * (1 - _MARGIN),
                    }
                )
    return lines


def _drop_running_lines(lines: list[dict], n_pages: int) -> list[dict]:
    """Remove header/footer lines that repeat (page numbers aside) on half the pages."""
    if n_pages < 2:
        return lines

    def key(text: str) -> str:
        return re.sub(r"\d+", "#", text).strip().lower()

    pages_by_key: dict[str, set[int]] = {}
    for ln in lines:
        if ln["margin"]:
            pages_by_key.setdefault(key(ln["text"]), set()).add(ln["page"])
    threshold = max(2, (n_pages + 1) // 2)
    repeated = {k for k, pages in pages_by_key.items() if len(pages) >= threshold}
    return [ln for ln in lines if not (ln["margin"] and key(ln["text"]) in repeated)]


def _heading_candidate(ln: dict, body_size: float) -> tuple[str, str] | None:
    """(canonical name, text left over on the line) when the line reads as a heading."""
    text = ln["text"]
    if len(text) > 80:
        return None
    larger = ln["size"] >= body_size + 1.0
    caps = text.isupper() and sum(c.isalpha() for c in text) >= 3
    name = canonical_section(text)
    if name and (larger or ln["bold"] or caps):
        return name, ""
    # "Abstract Sharing data..." - the label is the first span, body text follows.
    first = ln["first"]
    label = first["text"].strip()
    rest = text[len(first["text"].lstrip()) :].strip() if text.startswith(label) else ""
    if rest and label != text:
        lname = canonical_section(label)
        first_bold = ("Bold" in first.get("font", "")) or (first.get("flags", 0) & 16)
        if lname and (first_bold or first["size"] >= body_size + 1.0 or label.isupper()):
            return lname, rest
    return None


def _pick_headings(lines: list[dict], body_size: float) -> dict[int, tuple[str, str]]:
    """Choose one line per canonical name: the first larger-than-body one if any, else the first."""
    candidates: dict[str, list[tuple[int, str, bool]]] = {}
    for i, ln in enumerate(lines):
        if ln["margin"]:
            continue
        hit = _heading_candidate(ln, body_size)
        if hit:
            candidates.setdefault(hit[0], []).append((i, hit[1], ln["size"] >= body_size + 1.0))
    chosen: dict[int, tuple[str, str]] = {}
    for name, hits in candidates.items():
        sized = [h for h in hits if h[2]]
        i, rest, _ = (sized or hits)[0]
        chosen[i] = (name, rest)
    return chosen


def _pdf_sections(doc) -> str | None:
    """Section-marked text, or None when the PDF shows no standard headings."""
    lines = _drop_running_lines(_pdf_lines(doc), len(doc))
    if not lines:
        return None
    sizes = Counter()
    for ln in lines:
        sizes[round(ln["size"], 1)] += len(ln["text"])
    body_size = sizes.most_common(1)[0][0]
    headings = _pick_headings(lines, body_size)
    body_names = {name for name, _ in headings.values() if name != "references"}
    if not body_names:
        return None

    cut = None
    ref_positions = sorted(i for i, (name, _) in headings.items() if name == "references")
    total = sum(len(ln["text"]) for ln in lines)
    for i in ref_positions:
        before = sum(len(ln["text"]) for ln in lines[:i])
        if any(j < i for j in headings) or before >= total / 2:
            cut = i
            break

    segments: list[tuple[str | None, str]] = [(None, "")]
    buf: list[str] = []
    skipping = False

    def flush() -> None:
        name, _ = segments[-1]
        segments[-1] = (name, "\n".join(buf))
        buf.clear()

    for i, ln in enumerate(lines):
        if cut is not None and i >= cut:
            break
        if i in headings:
            name, rest = headings[i]
            if name == "references":
                continue
            flush()
            segments.append((name, ""))
            skipping = False
            if rest:
                buf.append(rest)
            continue
        text = ln["text"]
        looks_heading = len(text) <= 80 and (ln["size"] >= body_size + 1.0 or ln["bold"])
        if looks_heading:
            skipping = is_skipped_section(text)
            if skipping:
                continue
        if not skipping:
            buf.append(text)
    flush()
    return _assemble(segments)


def extract_text(pdf: Path) -> str:
    try:
        doc = pymupdf.open(str(pdf))
    except Exception:
        return ""
    try:
        with doc:
            if doc.is_encrypted:
                return ""
            try:
                marked = _pdf_sections(doc)
            except Exception:
                marked = None
            if marked is not None:
                return marked
            parts: list[str] = []
            for page in doc:
                parts.append(page.get_text() or "")
            return "\n".join(parts)
    except Exception:
        return ""


def write_text(pdf: Path, dest: Path) -> int:
    text = extract_text(pdf)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return len(text.strip())


def is_readable(n_chars: int) -> bool:
    return n_chars >= TEXT_FLOOR
