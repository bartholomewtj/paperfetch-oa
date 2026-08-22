from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from papers.cache import TEXT_FLOOR, pdf_path, text_path, write_meta
from papers.extract import extract_jats
from papers.fetch import FetchError, download_pdf, fetch_bytes

EPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"


def resolve(doi: str, mailto: str) -> bool:
    """Search Europe PMC and fetch JATS text.

    Returns True only when text.txt (>= TEXT_FLOOR chars) and meta.json are written.
    Returns False on any error or miss to fall through to Unpaywall.
    """
    try:
        params = urllib.parse.urlencode(
            {
                "query": f'DOI:"{doi}"',
                "format": "json",
                "resultType": "core",
                "pageSize": "1",
            }
        )
        search_url = f"{EPMC_SEARCH_URL}?{params}"
        raw = fetch_bytes(search_url, mailto)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return False

        results = data.get("resultList", {}).get("result", [])
        if not isinstance(results, list) or not results:
            return False

        item = results[0]
        if not isinstance(item, dict):
            return False

        pmcid = item.get("pmcid")
        if not pmcid or not isinstance(pmcid, str):
            return False
        pmcid = pmcid.strip()
        if not pmcid:
            return False

        if item.get("isOpenAccess") != "Y" or item.get("inEPMC") != "Y":
            return False

        xml_url = EPMC_FULLTEXT_URL.format(pmcid=urllib.parse.quote(pmcid))
        xml_bytes = fetch_bytes(xml_url, mailto)
        xml_text = xml_bytes.decode("utf-8", errors="replace")
        text = extract_jats(xml_text)
        n_chars = len(text.strip())
        if n_chars < TEXT_FLOOR:
            return False

        # Success - write text
        txt_path = text_path(doi)
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(text, encoding="utf-8")

        # Optional PDF download
        pdf_url = _find_pdf_url(item)
        if pdf_url:
            try:
                download_pdf(pdf_url, pdf_path(doi), mailto)
            except FetchError:
                pass

        # Parse metadata fields
        source = str(item.get("source") or "")
        pub_type = item.get("pubType")
        is_preprint = source == "PPR"
        if not is_preprint and pub_type:
            if isinstance(pub_type, list):
                is_preprint = any("preprint" in str(pt).lower() for pt in pub_type)
            elif isinstance(pub_type, str):
                is_preprint = "preprint" in pub_type.lower()
        version = "preprint" if is_preprint else "publishedVersion"

        license_val = item.get("license") or None
        if isinstance(license_val, str) and not license_val.strip():
            license_val = None

        title = str(item.get("title") or "").strip()
        year = _parse_year(item.get("pubYear"))
        journal = _parse_journal(item)

        write_meta(
            doi,
            {
                "title": title,
                "resolver": "europepmc",
                "version": version,
                "license": license_val,
                "journal": journal or None,
                "year": year,
                "text_chars": n_chars,
                "pmcid": pmcid,
            },
        )
        return True
    except Exception:
        return False


def _find_pdf_url(item: dict) -> str | None:
    url_list = item.get("fullTextUrlList", {}).get("fullTextUrl", [])
    if isinstance(url_list, list):
        for u in url_list:
            if isinstance(u, dict):
                style = str(u.get("documentStyle") or "").strip().lower()
                u_url = str(u.get("url") or "").strip()
                if style == "pdf" and u_url:
                    return u_url
    return None


def _parse_year(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    return None


def _parse_journal(item: dict) -> str:
    journal_info = item.get("journalInfo")
    if isinstance(journal_info, dict):
        journal_dict = journal_info.get("journal")
        if isinstance(journal_dict, dict):
            return str(journal_dict.get("title") or "").strip()
        if isinstance(journal_dict, str):
            return journal_dict.strip()
    if "journalTitle" in item:
        return str(item.get("journalTitle") or "").strip()
    return ""
