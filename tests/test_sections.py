"""Section markers in text.txt and the `sections` field in meta.json.

Every route (PDF, Europe PMC XML, PMC HTML) writes the same shape:
a ``## Results`` line before each recognised section, a blank line after it,
and no reference list. A PDF with no detectable headings still extracts,
just with no markers.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_offline import write_pdf

from papers.cache import read_meta, text_path, write_meta
from papers.extract import canonical_section, extract_html, extract_jats, extract_text, write_text

FIXTURES = Path(__file__).parent / "fixtures"
PLOS_PDF = FIXTURES / "plos_pone_0000308.pdf"
MARKER = re.compile(r"^## (\w+)$", re.MULTILINE)


def markers(text: str) -> list[str]:
    return [m.group(1) for m in MARKER.finditer(text)]


def test_plos_pdf_has_section_markers():
    text = extract_text(PLOS_PDF)
    assert markers(text) == ["Introduction", "Results", "Discussion", "Methods"]
    # Marker line, blank line, then the section's first line of text.
    assert re.search(r"^## Results\n\n\S", text, re.MULTILINE)
    # Front matter (title, authors, abstract) stays at the top without a marker.
    assert text.startswith("Sharing Detailed Research Data Is Associated with")
    assert text.index("Background. Sharing research data") < text.index("## Introduction")


def test_plos_pdf_drops_references_and_back_matter():
    text = extract_text(PLOS_PDF)
    assert "REFERENCES" not in text
    assert "Fienberg SE, Martin ME, Straf ML (1985)" not in text
    assert "ACKNOWLEDGMENTS" not in text
    assert "Conceived and designed the experiments" not in text
    assert "SUPPORTING INFORMATION" not in text
    # Body text up to the end of Methods survives.
    assert text.rstrip().endswith("values are two-tailed.")
    assert len(text) > 20_000


def test_plos_pdf_drops_running_headers_and_footers():
    text = extract_text(PLOS_PDF)
    assert "www.plosone.org" not in text
    assert "Sharing Data Citation Rate" not in text
    assert "March 2007 | Issue 3 | e308" not in text


def test_pdf_without_headings_extracts_plain(tmp_path):
    pdf = write_pdf(tmp_path / "plain.pdf", "Clinical note with no headings. " * 40)
    text = extract_text(pdf)
    assert "Clinical note with no headings." in text
    assert markers(text) == []


def test_meta_records_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    doi = "10.1371/journal.pone.0000308"
    n = write_text(PLOS_PDF, text_path(doi))
    write_meta(doi, {"title": "PLOS", "resolver": "unpaywall", "text_chars": n})
    assert read_meta(doi)["sections"] == ["introduction", "results", "discussion", "methods"]

    plain_doi = "10.1000/plain"
    pdf = write_pdf(tmp_path / "plain.pdf", "No headings here. " * 60)
    n = write_text(pdf, text_path(plain_doi))
    write_meta(plain_doi, {"title": "Plain", "resolver": "unpaywall", "text_chars": n})
    assert read_meta(plain_doi)["sections"] == []


def test_meta_keeps_caller_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    write_meta("10.1000/x", {"title": "x", "sections": ["results"]})
    assert read_meta("10.1000/x")["sections"] == ["results"]


def test_jats_markers_match_pdf_shape():
    xml = (FIXTURES / "epmc_fulltext.xml").read_text(encoding="utf-8")
    text = extract_jats(xml)
    assert markers(text) == ["Introduction", "Methods"]
    assert text.startswith("## Introduction\n\nEPMC_FULLTEXT_MARKER")
    assert re.search(r"^## Methods\n\n\S", text, re.MULTILINE)
    assert "Acknowledgments" not in text
    assert "should be skipped by the parser" not in text


def test_html_markers_match_pdf_shape():
    html = (FIXTURES / "pmc_article_manuscript.html").read_text(encoding="utf-8")
    text = extract_html(html)
    assert markers(text) == ["Abstract", "Methods", "Results", "Discussion"]
    assert re.search(r"^## Abstract\n\n\S", text, re.MULTILINE)
    assert "REFERENCES" not in text
    assert "Acknowledgments" not in text
    # A subsection heading we do not recognise stays in the body as a plain line.
    assert "\nGenotyping\n" in text


def test_canonical_section_names():
    cases = {
        "Abstract": "abstract",
        "1. INTRODUCTION": "introduction",
        "Background": "introduction",
        "2. MATERIALS AND METHODS": "methods",
        "Patients and methods": "methods",
        "Methods:": "methods",
        "3 | Results": "results",
        "Results and Discussion": "results",
        "IV. Discussion": "discussion",
        "Conclusion": "conclusions",
        "References": "references",
        "Literature Cited": "references",
        "Bibliography": "references",
        "Patient characteristics": None,
        "Genotyping": None,
        "": None,
    }
    for title, expected in cases.items():
        assert canonical_section(title) == expected, title
