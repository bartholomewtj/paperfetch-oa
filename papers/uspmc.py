"""US PubMed Central (NCBI) resolver."""

from __future__ import annotations

import json
import re
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
import tarfile

from papers.cache import TEXT_FLOOR, meta_path, pdf_path, text_path, write_meta
from papers.extract import looks_like_pdf, write_text
from papers.fetch import MAX_PDF_BYTES, FetchError, download_pdf, fetch_bytes


FTP_PMC = "ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/"
HTTPS_PMC = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/"
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


def _http_url(href: str) -> str:
    if href.startswith("ftp://"):
        return "https://" + href[len("ftp://"):]
    return href


def _http_urls(href: str) -> list[str]:
    """URLs to try for an oa.fcgi link, in order.

    oa.fcgi still hands out ftp:// paths under pub/pmc/, but NCBI moved the
    legacy trees (oa_package/, oa_pdf/, manuscript/) to pub/pmc/deprecated/
    in 2026 (readme dated 4/10/2026), so the plain https rewrite 404s.
    Try it anyway (in case the link is updated), then the deprecated tree.
    Long-term replacement is the AWS bucket (pmc.ncbi.nlm.nih.gov/tools/pmcaws/).
    """
    first = _http_url(href)
    urls = [first]
    if first.startswith(HTTPS_PMC) and "/deprecated/" not in first:
        urls.append(HTTPS_PMC + "deprecated/" + first[len(HTTPS_PMC):])
    return urls


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

        # Step B2: AWS Open Access bucket (successor to the deprecated FTP trees)
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

        # Step C: OA web service (legacy; the ftp trees are slated for removal)
        oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={urllib.parse.quote(pmcid)}"
        try:
            xml_raw = fetch_bytes(oa_url, mailto)
            root = ET.fromstring(xml_raw)
        except Exception:
            _cleanup()
            return False

        links = root.findall(".//link")
        pdf_link: str | None = None
        tgz_link: str | None = None

        for link in links:
            fmt = (link.attrib.get("format") or "").strip().lower()
            href = (link.attrib.get("href") or "").strip()
            if not href:
                continue
            if fmt == "pdf" and pdf_link is None:
                pdf_link = href
            elif fmt == "tgz" and tgz_link is None:
                tgz_link = href

        if pdf_link is not None:
            try:
                n = 0
                for candidate in _http_urls(pdf_link):
                    try:
                        download_pdf(candidate, dest_pdf, mailto)
                    except Exception:
                        continue
                    n = write_text(dest_pdf, dest_txt)
                    break
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
            return False

        if tgz_link is not None:
            tmp_path: Path | None = None
            try:
                raw_tgz = None
                for candidate in _http_urls(tgz_link):
                    try:
                        raw_tgz = fetch_bytes(candidate, mailto)
                        break
                    except Exception:
                        continue
                if raw_tgz is None:
                    raise FetchError("tgz unavailable")
                fd, tmp_name = tempfile.mkstemp()
                tmp_path = Path(tmp_name)
                with open(fd, "wb") as f:
                    f.write(raw_tgz)
                with tarfile.open(tmp_path, "r:*") as tar:
                    for member in tar.getmembers():
                        if not member.isfile():
                            continue
                        name = member.name
                        if ".." in name or name.startswith("/") or name.startswith("\\"):
                            continue
                        if name.lower().endswith(".pdf"):
                            extracted = tar.extractfile(member)
                            if extracted is not None:
                                pdf_bytes = extracted.read()
                                if looks_like_pdf(pdf_bytes) and len(pdf_bytes) <= MAX_PDF_BYTES:
                                    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
                                    dest_pdf.write_bytes(pdf_bytes)
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
                            break
            except Exception:
                pass
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            _cleanup()
            return False

        _cleanup()
        return False
    except Exception:
        _cleanup()
        return False
