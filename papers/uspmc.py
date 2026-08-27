"""US PubMed Central (NCBI) resolver."""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from papers.cache import TEXT_FLOOR, meta_path, pdf_path, text_path, write_meta
from papers.extract import extract_html, write_text
from papers.fetch import download_pdf, fetch_bytes


AWS_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com/"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def aws_pdf_key(listing_xml: bytes, pmcid: str) -> str | None:
    """Pick the newest-version PDF key from an S3 ListObjectsV2 response.

    The PMC Open Access bucket stores each article as PMCxxx.N/PMCxxx.N.pdf
    (N = article version). Return the key with the highest N, or None.
    """
    try:
        root = ET.fromstring(listing_xml)
    except ET.ParseError:
        return None
    best: tuple[int, str] | None = None
    pat = re.compile(rf"^{re.escape(pmcid)}\.(\d+)/{re.escape(pmcid)}\.(\d+)\.pdf$")
    for key_el in root.iter(f"{_S3_NS}Key"):
        key = (key_el.text or "").strip()
        m = pat.match(key)
        if m and m.group(1) == m.group(2):
            ver = int(m.group(1))
            if best is None or ver > best[0]:
                best = (ver, key)
    return best[1] if best else None


def _parse_title(raw: object) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _parse_journal(raw: object) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    return val if val else None


def _parse_year(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    return None


def resolve(doi: str, mailto: str) -> bool:
    """Resolve a DOI via US PMC.

    Returns True only when a readable PDF (>= TEXT_FLOOR chars) and meta.json are written.
    Returns False on any error or miss so the ladder falls through.
    """
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
        # Step A: ID Converter
        params = urllib.parse.urlencode(
            {
                "ids": doi,
                "format": "json",
                "tool": "paperfetch",
                "email": mailto,
            }
        )
        idconv_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?{params}"
        raw = fetch_bytes(idconv_url, mailto)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _cleanup()
            return False

        if not isinstance(data, dict):
            _cleanup()
            return False

        status = data.get("status")
        if isinstance(status, str) and status.strip().lower() == "error":
            _cleanup()
            return False

        records = data.get("records")
        if not isinstance(records, list) or not records:
            _cleanup()
            return False

        rec = records[0]
        if not isinstance(rec, dict):
            _cleanup()
            return False

        rec_status = rec.get("status")
        if isinstance(rec_status, str) and rec_status.strip().lower() == "error":
            _cleanup()
            return False

        pmcid = rec.get("pmcid")
        if not isinstance(pmcid, str) or not pmcid.strip():
            _cleanup()
            return False
        pmcid = pmcid.strip()

        title = _parse_title(rec.get("title"))
        journal = _parse_journal(rec.get("journal"))
        year = _parse_year(rec.get("year"))

        # Step B: direct PMC PDF
        direct_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{urllib.parse.quote(pmcid)}/pdf/"
        try:
            download_pdf(direct_url, dest_pdf, mailto)
            n = write_text(dest_pdf, dest_txt)
            if n >= TEXT_FLOOR:
                write_meta(
                    doi,
                    {
                        "title": title,
                        "resolver": "uspmc",
                        "journal": journal,
                        "year": year,
                        "text_chars": n,
                        "pmcid": pmcid,
                    },
                )
                return True
        except Exception:
            pass

        _cleanup()

        # Step B2: AWS Open Access bucket (successor to the legacy FTP trees)
        try:
            listing_url = (
                AWS_BUCKET
                + "?list-type=2&prefix="
                + urllib.parse.quote(pmcid + ".")
            )
            key = aws_pdf_key(fetch_bytes(listing_url, mailto), pmcid)
            if key:
                download_pdf(AWS_BUCKET + key, dest_pdf, mailto)
                n = write_text(dest_pdf, dest_txt)
                if n >= TEXT_FLOOR:
                    write_meta(
                        doi,
                        {
                            "title": title,
                            "resolver": "uspmc",
                            "journal": journal,
                            "year": year,
                            "text_chars": n,
                            "pmcid": pmcid,
                        },
                    )
                    return True
        except Exception:
            pass

        _cleanup()

        # Step B3: the article page itself. Author manuscripts (NIHMS deposits)
        # are in PMC but not in its Open Access subset: Europe PMC has no XML
        # for them, the AWS bucket does not carry them, and the PMC PDF link
        # sits behind a browser proof-of-work challenge. The HTML page serves
        # the full text to a plain client, so read that. No PDF is written.
        try:
            page_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{urllib.parse.quote(pmcid)}/"
            html_text = fetch_bytes(page_url, mailto).decode("utf-8", errors="replace")
            text = extract_html(html_text)
            n = len(text.strip())
            if n >= TEXT_FLOOR:
                dest_txt.parent.mkdir(parents=True, exist_ok=True)
                dest_txt.write_text(text, encoding="utf-8")
                meta = {
                    "title": title,
                    "resolver": "uspmc",
                    "journal": journal,
                    "year": year,
                    "text_chars": n,
                    "pmcid": pmcid,
                }
                if "author manuscript" in html_text.lower():
                    meta["version"] = "authorManuscript"
                write_meta(doi, meta)
                return True
        except Exception:
            pass

        _cleanup()
        return False
    except Exception:
        _cleanup()
        return False
