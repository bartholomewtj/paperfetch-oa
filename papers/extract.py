from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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
