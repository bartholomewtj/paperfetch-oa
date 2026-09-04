from __future__ import annotations

import argparse
import json
import os
import sys

from papers.cache import (
    MAX_CHARS,
    TEXT_FLOOR,
    cache_inventory,
    cache_ok,
    cache_root,
    looks_like_doi,
    meta_path,
    normalize_doi,
    pdf_path,
    read_meta,
    text_chars,
    text_path,
    write_meta,
)
from papers.core import resolve as core_resolve
from papers.crossref import resolve_title as crossref_resolve_title
from papers.europepmc import resolve as europepmc_resolve
from papers.extract import write_text
from papers.fetch import FetchError, download_pdf
from papers.openalex import resolve as openalex_resolve
from papers.preprints import resolve as preprint_resolve
from papers.semanticscholar import resolve as s2_resolve
from papers.unpaywall import LookupError, lookup
from papers.uspmc import resolve as uspmc_resolve


class UsageError(Exception):
    pass


def _mailto() -> str:
    value = (os.environ.get("PAPERS_MAILTO") or "").strip()
    if not value:
        raise UsageError("set PAPERS_MAILTO to your email")
    return value


def _ok_record(doi: str, meta: dict, n_chars: int) -> dict:
    read = str(text_path(doi).resolve())
    return {
        "status": "ok",
        "doi": doi,
        "title": meta.get("title") or "",
        "resolver": meta.get("resolver") or "unpaywall",
        "version": meta.get("version"),
        "license": meta.get("license"),
        "read": read,
        "max_chars": MAX_CHARS,
        "text_chars": n_chars,
        "agent_next": "read_text; cite_doi_and_version; do_not_attach_pdf",
    }


def _no_oa_record(doi: str, title: str, tried: str) -> dict:
    return {
        "status": "no_oa",
        "doi": doi,
        "title": title or "",
        "tried": tried,
        "agent_next": "stop_fetch; abstract_only",
    }


def _unreadable_record(doi: str, n_chars: int, title: str = "") -> dict:
    return {
        "status": "unreadable_pdf",
        "doi": doi,
        "title": title,
        "text_chars": n_chars,
        "agent_next": "notify_human; do_not_cite_as_read",
    }


def _retry_record(doi: str) -> dict:
    return {
        "status": "retry",
        "doi": doi,
        "agent_next": "stop_fetch; try_later; abstract_only",
    }


def _config_error_record(reason: str) -> dict:
    return {
        "status": "config_error",
        "reason": reason,
        "agent_next": "notify_human; stop_fetch",
    }


def get_paper(raw: str) -> tuple[dict, int]:
    if not looks_like_doi(raw):
        mailto = _mailto()
        resolved_doi = crossref_resolve_title(raw, mailto)
        if not resolved_doi:
            return {
                "status": "no_doi",
                "agent_next": "notify_human",
            }, 1
        print(f"resolved title -> {resolved_doi}", file=sys.stderr)
        raw_doi = resolved_doi
    else:
        raw_doi = raw

    doi = normalize_doi(raw_doi)
    pdf = pdf_path(doi)
    txt = text_path(doi)
    meta = read_meta(doi)

    if cache_ok(doi):
        return _ok_record(doi, meta, text_chars(doi)), 0

    if pdf.is_file():
        n = text_chars(doi)
        if not txt.is_file():
            n = write_text(pdf, txt)
            if n >= TEXT_FLOOR:
                meta = read_meta(doi)
                return _ok_record(doi, meta, n), 0
        return _unreadable_record(doi, n, meta.get("title") or ""), 2

    mailto = _mailto()
    tried: list[str] = []
    if europepmc_resolve(doi, mailto):
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0
    tried.append("europepmc")

    uspmc_res = uspmc_resolve(doi, mailto)
    if uspmc_res is True:
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0

    # Unpaywall: an API error or a blocked publisher PDF must not end the run —
    # OpenAlex / S2 / preprints / CORE may still hold a copy.
    result = None
    unpaywall_error = False
    try:
        result = lookup(doi, mailto)
    except LookupError:
        unpaywall_error = True

    title = result.title if result else ""
    if uspmc_res is False:
        tried.append("uspmc")
    locations = list(result.locations) if result else []
    if result and result.pdf_url and not locations:
        locations = [(result.pdf_url, result.license, result.version)]

    def _drop_unpaywall() -> None:
        for p in (pdf, txt, meta_path(doi)):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    for url, lic, version in locations:
        try:
            download_pdf(url, pdf, mailto)
        except FetchError:
            _drop_unpaywall()
            continue
        n = write_text(pdf, txt)
        write_meta(
            doi,
            {
                "title": title,
                "resolver": "unpaywall",
                "version": version,
                "license": lic,
                "journal": result.journal,
                "year": result.year,
                "text_chars": n,
            },
        )
        if n >= TEXT_FLOOR:
            return _ok_record(doi, read_meta(doi), n), 0
        # Short extract: drop it and keep going (next Unpaywall URL, then
        # OpenAlex / S2 / CORE). A scan should not hide a later readable copy.
        _drop_unpaywall()
        continue
    if not unpaywall_error:
        tried.append("unpaywall_blocked" if locations else "unpaywall")

    oa_res = openalex_resolve(doi, mailto)
    if oa_res in ("hit", True):
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0
    if oa_res == "unreadable":
        return _unreadable_record(doi, text_chars(doi), (read_meta(doi).get("title") or title)), 2
    if oa_res in ("miss", False):
        tried.append("openalex")

    s2_res = s2_resolve(doi, mailto)
    if s2_res in ("hit", True):
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0
    if s2_res == "unreadable":
        return _unreadable_record(doi, text_chars(doi), (read_meta(doi).get("title") or title)), 2
    if s2_res in ("miss", False):
        tried.append("semanticscholar")

    pr_res = preprint_resolve(doi, mailto)
    if pr_res in ("hit", True):
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0
    if pr_res == "unreadable":
        return _unreadable_record(doi, text_chars(doi), (read_meta(doi).get("title") or title)), 2
    if pr_res in ("miss", False):
        tried.append("preprint")

    core_res = core_resolve(doi, mailto)
    if core_res in ("hit", True):
        return _ok_record(doi, read_meta(doi), text_chars(doi)), 0
    if core_res == "unreadable":
        return _unreadable_record(doi, text_chars(doi), (read_meta(doi).get("title") or title)), 2
    if core_res in ("miss", False):
        tried.append("core")

    if unpaywall_error:
        # Unpaywall was unreachable, so we don't know if this is OA.
        return _retry_record(doi), 2

    tried_str = ",".join(tried)
    return _no_oa_record(doi, title, tried_str), 2


def _get_inputs(items: list[str]) -> list[str]:
    """Expand the `get` arguments. `-` means one DOI or title per stdin line."""
    out: list[str] = []
    for item in items:
        if item != "-":
            out.append(item)
            continue
        for line in sys.stdin:
            line = line.strip()
            if line:
                out.append(line)
    if not out:
        raise UsageError("no DOI or title given")
    return out


def get_many(items: list[str]) -> int:
    """Run get_paper for each input in one process, one JSON line each.

    One input behaves exactly like before. For more than one, the exit code is
    0 only if every line is ok, else 2. Per-process memos (the Semantic Scholar
    skip, the Unpaywall error memo) carry across the whole batch.
    """
    inputs = _get_inputs(items)
    codes: list[int] = []
    for raw in inputs:
        record, code = get_paper(raw)
        print(json.dumps(record), flush=True)
        codes.append(code)
    if len(codes) == 1:
        return codes[0]
    return 0 if all(c == 0 for c in codes) else 2


def status_report() -> dict:
    try:
        inv = cache_inventory()
    except Exception:
        inv = {"cached": {"count": 0, "chars": 0}, "unreadable": {"count": 0}}
    try:
        c_root = str(cache_root())
    except Exception:
        c_root = ""
    mailto_set = bool((os.environ.get("PAPERS_MAILTO") or "").strip())
    s2_key_set = bool((os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip())
    return {
        "cached": inv.get("cached", {"count": 0, "chars": 0}),
        "unreadable": inv.get("unreadable", {"count": 0}),
        "cache_root": c_root,
        "mailto_set": mailto_set,
        "s2_key_set": s2_key_set,
        "core_key_set": bool((os.environ.get("CORE_API_KEY") or "").strip()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="papers",
        description="Get open-access full text for a DOI or title.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    get_p = sub.add_parser("get", help="Fetch one or more papers by DOI or title")
    get_p.add_argument(
        "doi",
        nargs="+",
        help="DOI or paper title (repeatable); '-' reads one per line from stdin",
    )

    st_p = sub.add_parser("status", help="Show local cache counts as JSON")
    st_p.add_argument("--json", action="store_true", help="Print JSON (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        return 1
    try:
        if args.cmd == "status":
            print(json.dumps(status_report()), flush=True)
            return 0
        if args.cmd == "get":
            return get_many(args.doi)
        parser.print_help()
        return 1
    except UsageError as exc:
        if args.cmd == "get":
            print(json.dumps(_config_error_record(str(exc))), flush=True)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
