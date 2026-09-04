from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from papers.cache import (
    cache_ok,
    cache_root,
    folder_key,
    normalize_doi,
    paper_dir,
    pdf_path,
    read_meta,
    text_chars,
    text_path,
    write_meta,
)
from papers.cli import main
from papers.extract import extract_html, extract_jats, extract_text, looks_like_pdf, write_jats_text
from papers.fetch import FetchError, SafeRedirectHandler, UnsafeUrl, assert_url_safe, download_pdf
from papers.unpaywall import Lookup, LookupError, lookup

FIXTURES = Path(__file__).parent / "fixtures"
SICI = "10.1002/(SICI)1097-4571(1997)48:1<17::AID-ASI3>3.0.CO;2-4"
PLOS = "10.1371/journal.pone.0000308"
CLOSED = "10.1001/jamapsychiatry.2018.1776"


def build_pdf_bytes(text: str) -> bytes:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    chunks = [escaped[i : i + 80] for i in range(0, len(escaped), 80)] or [""]
    ops = ["BT /F1 10 Tf 40 750 Td"]
    for i, chunk in enumerate(chunks):
        if i:
            ops.append("0 -14 Td")
        ops.append(f"({chunk}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", "replace")

    def obj(n: int, body: bytes) -> bytes:
        return f"{n} 0 obj\n".encode() + body + b"\nendobj\n"

    objects = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        ),
        obj(4, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"),
        obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ]
    header = b"%PDF-1.4\n"
    body = b""
    offsets = [0]
    for item in objects:
        offsets.append(len(header) + len(body))
        body += item
    xref_pos = len(header) + len(body)
    xref = [b"xref\n", f"0 {len(objects) + 1}\n".encode(), b"0000000000 65535 f \n"]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode())
    trailer = (
        b"trailer\n"
        + f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
        + b"startxref\n"
        + f"{xref_pos}\n".encode()
        + b"%%EOF\n"
    )
    return header + body + b"".join(xref) + trailer


def write_pdf(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_pdf_bytes(text))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PAPERS_MAILTO", "tester@example.test")
    return tmp_path


@pytest.fixture(autouse=True)
def epmc_default_miss(monkeypatch):
    monkeypatch.setattr("papers.cli.europepmc_resolve", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def uspmc_default_skip(monkeypatch):
    monkeypatch.setattr("papers.cli.uspmc_resolve", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def openalex_default_skip(monkeypatch):
    monkeypatch.setattr("papers.cli.openalex_resolve", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def preprint_default_skip(monkeypatch):
    monkeypatch.setattr("papers.cli.preprint_resolve", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def s2_default_skip(monkeypatch):
    monkeypatch.setattr("papers.cli.s2_resolve", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def core_default_skip(monkeypatch):
    monkeypatch.setattr("papers.cli.core_resolve", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def reset_s2_process_skip():
    import papers.semanticscholar as s2

    s2._skip_for_process = False
    yield
    s2._skip_for_process = False


def run(capsys, argv):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def seed_ok(doi: str, title: str = "Seed paper") -> None:
    body = ("Readable full text. " * 40).strip()
    write_pdf(pdf_path(doi), body)
    text_path(doi).parent.mkdir(parents=True, exist_ok=True)
    text_path(doi).write_text(body, encoding="utf-8")
    write_meta(
        doi,
        {
            "title": title,
            "resolver": "unpaywall",
            "version": "publishedVersion",
            "license": "cc-by",
            "text_chars": len(body),
        },
    )


def test_pdf_builder_extracts(tmp_path):
    path = tmp_path / "mark.pdf"
    path.write_bytes(build_pdf_bytes("HELLO_MARKER " * 5))
    assert path.read_bytes().startswith(b"%PDF")
    assert "HELLO_MARKER" in extract_text(path)


def test_doi_spellings_share_one_key(home):
    variants = [
        "10.1371/journal.pone.0000308",
        "https://doi.org/10.1371/journal.pone.0000308",
        "http://dx.doi.org/10.1371/JOURNAL.PONE.0000308",
        "DOI:10.1371/journal.pone.0000308",
        "doi: 10.1371/journal.pone.0000308",
    ]
    keys = {folder_key(normalize_doi(v)) for v in variants}
    assert keys == {"10.1371%2Fjournal.pone.0000308"}


def test_sici_doi_stays_in_cache(home):
    doi = normalize_doi(SICI)
    folder = paper_dir(doi)
    assert folder.parent == cache_root() / "cache"
    assert "<" not in folder.name
    assert ">" not in folder.name
    assert ":" not in folder.name
    assert '"' not in folder.name


def test_cache_hit_skips_network(home, capsys, monkeypatch):
    seed_ok(PLOS)

    def boom(*a, **k):
        raise AssertionError("network should not run")

    monkeypatch.setattr("papers.cli.lookup", boom)
    monkeypatch.setattr("papers.cli.download_pdf", boom)
    code, out, _ = run(capsys, ["get", "https://doi.org/" + PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == PLOS


def test_not_oa_queues_once(home, capsys, monkeypatch):
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )
    first, out1, _ = run(capsys, ["get", CLOSED])
    second, out2, _ = run(capsys, ["get", CLOSED])
    assert first == 2 and second == 2
    assert json.loads(out1)["status"] == "no_oa"
    assert json.loads(out2)["status"] == "no_oa"


def test_empty_text_pdf_is_not_ok(home, capsys, monkeypatch):
    empty = home / "empty.pdf"
    write_pdf(empty, "")
    assert extract_text(empty).strip() == ""
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(True, "https://files.example/paper.pdf", "Empty", "", 2007, "cc-by", "publishedVersion"),
    )

    def fake_dl(url, dest, mailto):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(empty.read_bytes())

    monkeypatch.setattr("papers.cli.download_pdf", fake_dl)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert any(x.startswith("unpaywall") for x in rec["tried"].split(","))
    assert not pdf_path(PLOS).exists()


def test_unpaywall_5xx_retry_no_queue(home, capsys, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url,
            503,
            "unavailable",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(LookupError):
        lookup(PLOS, "tester@example.test")
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "retry"
    assert rec["agent_next"] == "stop_fetch; try_later; abstract_only"
    assert not pdf_path(PLOS).exists()


def test_read_path_is_under_home_paperfetch(home, capsys):
    seed_ok(PLOS)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    read = Path(rec["read"])
    assert read.is_file()
    root = (Path.home() / ".paperfetch").resolve()
    assert str(read.resolve()).startswith(str(root))
    assert rec["max_chars"] == 12000
    assert rec["agent_next"] == "read_text; cite_doi_and_version; do_not_attach_pdf"


def test_ssrf_file_url():
    with pytest.raises(UnsafeUrl):
        assert_url_safe("file:///etc/passwd")


def test_ssrf_loopback():
    with pytest.raises(UnsafeUrl):
        assert_url_safe("http://127.0.0.1/secret")


def test_ssrf_metadata():
    with pytest.raises(UnsafeUrl):
        assert_url_safe("http://169.254.169.254/latest/meta-data")


def test_ssrf_ipv6_loopback():
    with pytest.raises(UnsafeUrl):
        assert_url_safe("http://[::1]/secret")


def test_ssrf_localhost():
    with pytest.raises(UnsafeUrl):
        assert_url_safe("http://localhost/x")


def test_ssrf_dns_fail_closed(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(UnsafeUrl):
        assert_url_safe("https://does-not-resolve.invalid/x")


def test_ssrf_redirect_to_loopback():
    handler = SafeRedirectHandler()
    req = urllib.request.Request("https://example.org/paper.pdf")
    with pytest.raises(UnsafeUrl):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/secret")


def test_redirect_loopback_get_falls_through_no_cache(home, capsys, monkeypatch):
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(
            True, "https://files.example/paper.pdf", "Title", "", 2007, "cc-by", "publishedVersion"
        ),
    )

    def fake_dl(url, dest, mailto):
        assert_url_safe("http://127.0.0.1/secret")

    monkeypatch.setattr("papers.cli.download_pdf", fake_dl)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"].startswith("europepmc,unpaywall_blocked")
    assert not pdf_path(PLOS).exists()
    assert not (cache_root() / "cache").exists() or not any((cache_root() / "cache").iterdir())


def test_download_html_rejected(home, monkeypatch):
    monkeypatch.setattr("papers.fetch.assert_url_safe", lambda url: None)

    class FakeResp:
        def __init__(self):
            self.headers = {}
            self._data = b"<!DOCTYPE html><html></html>"
            self._off = 0

        def read(self, n=-1):
            if self._off >= len(self._data):
                return b""
            chunk = self._data[self._off : self._off + (n if n > 0 else len(self._data))]
            self._off += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, req, timeout=None):
            return FakeResp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener())
    dest = pdf_path(PLOS)
    with pytest.raises(Exception):
        download_pdf("https://files.example/paper.pdf", dest, "tester@example.test")
    assert not dest.exists()


def test_help_names_only_slice1_commands(capsys):
    assert main(["-h"]) == 0
    out = capsys.readouterr().out
    assert "get" in out
    assert "status" in out
    assert "miss" not in out
    assert "ingest" not in out
    lowered = out.lower()
    assert "europe pmc" not in lowered
    assert "openalex" not in lowered
    assert "semantic scholar" not in lowered


def test_usage_missing_mailto(home, capsys, monkeypatch):
    monkeypatch.delenv("PAPERS_MAILTO", raising=False)
    code, out, err = run(capsys, ["get", PLOS])
    assert code == 1
    assert err == ""
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "status": "config_error",
        "reason": "set PAPERS_MAILTO to your email",
        "agent_next": "notify_human; stop_fetch",
    }


def test_html_as_pdf_falls_through_to_queue(home, capsys, monkeypatch):
    from papers.fetch import FetchError

    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(True, "https://files.example/paper.pdf", "T", "", 2007, None, None),
    )

    def reject(url, dest, mailto):
        raise FetchError("not a PDF")

    monkeypatch.setattr("papers.cli.download_pdf", reject)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert rec["status"] == "no_oa"
    assert rec["tried"].startswith("europepmc,unpaywall_blocked")
    assert not pdf_path(PLOS).exists()


def test_publisher_pdf_blocked_next_resolver_still_runs(home, capsys, monkeypatch):
    """A blocked publisher PDF must not stop OpenAlex from being tried."""
    from papers.fetch import FetchError

    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(True, "https://www.nature.com/x.pdf", "T", "Nature", 2013, None, None),
    )

    def reject(url, dest, mailto):
        raise FetchError("not a PDF")

    monkeypatch.setattr("papers.cli.download_pdf", reject)
    called = []

    def oa_hit(doi, mailto):
        called.append(doi)
        write_pdf(pdf_path(doi), "full text " * 400)
        from papers.cache import text_path
        from papers.extract import write_text
        write_text(pdf_path(doi), text_path(doi))
        write_meta(doi, {"title": "T", "resolver": "openalex", "text_chars": 4000})
        return "hit"

    monkeypatch.setattr("papers.cli.openalex_resolve", oa_hit)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert called == [PLOS]
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "openalex"


def test_unpaywall_short_pdf_falls_through_to_openalex(home, capsys, monkeypatch):
    """A PDF with no extractable text must not stop OpenAlex from being tried."""
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(
            True, "https://files.example/scan.pdf", "T", "Nature", 2013, None, None
        ),
    )

    def short_dl(url, dest, mailto):
        write_pdf(dest, "")

    monkeypatch.setattr("papers.cli.download_pdf", short_dl)
    called = []

    def oa_hit(doi, mailto):
        called.append(doi)
        write_pdf(pdf_path(doi), "full text " * 400)
        from papers.extract import write_text as _wt
        _wt(pdf_path(doi), text_path(doi))
        write_meta(doi, {"title": "T", "resolver": "openalex", "text_chars": 4000})
        return "hit"

    monkeypatch.setattr("papers.cli.openalex_resolve", oa_hit)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert called == [PLOS]
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "openalex"


def test_unpaywall_second_location_used_when_first_blocked(home, capsys, monkeypatch):
    from papers.fetch import FetchError

    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(
            True,
            "https://www.nature.com/x.pdf",
            "T",
            "Nature",
            2013,
            None,
            "publishedVersion",
            locations=[
                ("https://www.nature.com/x.pdf", None, "publishedVersion"),
                ("https://europepmc.org/x.pdf", "cc-by", "acceptedVersion"),
            ],
        ),
    )
    seen = []

    def dl(url, dest, mailto):
        seen.append(url)
        if "nature.com" in url:
            raise FetchError("not a PDF")
        write_pdf(dest, "full text " * 400)

    monkeypatch.setattr("papers.cli.download_pdf", dl)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert seen == ["https://www.nature.com/x.pdf", "https://europepmc.org/x.pdf"]
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["version"] == "acceptedVersion"
    assert rec["license"] == "cc-by"


def test_unpaywall_locations_repository_first():
    from papers.unpaywall import _locations

    payload = {
        "best_oa_location": {"url_for_pdf": "https://pub/x.pdf", "host_type": "publisher", "version": "publishedVersion"},
        "oa_locations": [
            {"url_for_pdf": "https://pub/x.pdf", "host_type": "publisher", "version": "publishedVersion"},
            {"url_for_pdf": "https://pmc/x.pdf", "host_type": "repository", "version": "acceptedVersion", "license": "cc-by"},
            {"url_for_pdf": None, "host_type": "repository"},
        ],
    }
    assert _locations(payload) == [
        ("https://pmc/x.pdf", "cc-by", "acceptedVersion"),
        ("https://pub/x.pdf", None, "publishedVersion"),
    ]


def test_epmc_hit_ok_unpaywall_never_called(home, capsys, monkeypatch):
    import papers.europepmc

    search_json = (FIXTURES / "epmc_search_oa.json").read_bytes()
    fulltext_xml = (FIXTURES / "epmc_fulltext.xml").read_bytes()

    def fake_fetch_bytes(url, mailto):
        if "europepmc/webservices/rest/search" in url:
            return search_json
        if "fullTextXML" in url:
            return fulltext_xml
        raise AssertionError(f"unexpected url: {url}")

    def fake_download_pdf(url, dest, mailto):
        write_pdf(dest, "Mock PDF contents for EPMC article")

    monkeypatch.setattr("papers.europepmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.europepmc.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)

    def unpaywall_boom(*a, **k):
        raise AssertionError("Unpaywall should never be called on EPMC hit")

    monkeypatch.setattr("papers.cli.lookup", unpaywall_boom)

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == PLOS
    assert rec["resolver"] == "europepmc"
    assert rec["version"] == "publishedVersion"
    assert rec["read"] == str(text_path(PLOS).resolve())
    assert rec["max_chars"] == 12000
    assert rec["agent_next"] == "read_text; cite_doi_and_version; do_not_attach_pdf"
    assert text_path(PLOS).is_file()
    assert rec["text_chars"] >= 500
    assert pdf_path(PLOS).is_file()


def test_epmc_preprint_version(home, capsys, monkeypatch):
    import papers.europepmc

    search_data = json.loads((FIXTURES / "epmc_search_oa.json").read_text(encoding="utf-8"))
    search_data["resultList"]["result"][0]["source"] = "PPR"
    search_json = json.dumps(search_data).encode("utf-8")
    fulltext_xml = (FIXTURES / "epmc_fulltext.xml").read_bytes()

    def fake_fetch_bytes(url, mailto):
        if "europepmc/webservices/rest/search" in url:
            return search_json
        if "fullTextXML" in url:
            return fulltext_xml
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("papers.europepmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "europepmc"
    assert rec["version"] == "preprint"


def test_epmc_hit_ok_without_pdf(home, capsys, monkeypatch):
    import papers.europepmc

    # OA search hit without fullTextUrlList PDF
    search_data = json.loads((FIXTURES / "epmc_search_oa.json").read_text(encoding="utf-8"))
    search_data["resultList"]["result"][0]["fullTextUrlList"]["fullTextUrl"] = []
    search_json = json.dumps(search_data).encode("utf-8")
    fulltext_xml = (FIXTURES / "epmc_fulltext.xml").read_bytes()

    def fake_fetch_bytes(url, mailto):
        if "europepmc/webservices/rest/search" in url:
            return search_json
        if "fullTextXML" in url:
            return fulltext_xml
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("papers.europepmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr("papers.cli.lookup", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no unpaywall")))

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "europepmc"
    assert not pdf_path(PLOS).exists()
    assert text_path(PLOS).is_file()


def test_epmc_not_oa_falls_through_to_unpaywall(home, capsys, monkeypatch):
    import papers.europepmc

    search_json = (FIXTURES / "epmc_search_not_oa.json").read_bytes()
    monkeypatch.setattr("papers.europepmc.fetch_bytes", lambda url, mailto: search_json)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper title", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"
    assert not text_path(CLOSED).exists()


def test_epmc_not_inepmc_falls_through_to_unpaywall(home, capsys, monkeypatch):
    import papers.europepmc

    search_json = (FIXTURES / "epmc_search_not_inepmc.json").read_bytes()
    monkeypatch.setattr("papers.europepmc.fetch_bytes", lambda url, mailto: search_json)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Not in EPMC paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"


def test_epmc_5xx_falls_through_to_unpaywall(home, capsys, monkeypatch):
    import papers.europepmc

    def boom(url, mailto):
        raise FetchError("http 503")

    monkeypatch.setattr("papers.europepmc.fetch_bytes", boom)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"


def test_epmc_short_xml_falls_through_to_unpaywall(home, capsys, monkeypatch):
    import papers.europepmc

    search_json = (FIXTURES / "epmc_search_oa.json").read_bytes()
    short_xml = (FIXTURES / "epmc_fulltext_short.xml").read_bytes()

    def fake_fetch_bytes(url, mailto):
        if "europepmc/webservices/rest/search" in url:
            return search_json
        if "fullTextXML" in url:
            return short_xml
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("papers.europepmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Short text paper", "PLOS", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"
    assert not text_path(PLOS).exists()


def test_epmc_empty_search_falls_through_to_unpaywall(home, capsys, monkeypatch):
    import papers.europepmc

    empty_json = (FIXTURES / "epmc_search_empty.json").read_bytes()
    monkeypatch.setattr("papers.europepmc.fetch_bytes", lambda url, mailto: empty_json)
    monkeypatch.setattr("papers.cli.europepmc_resolve", papers.europepmc.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Unknown paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"


def test_xml_only_cache_hit_skips_both_resolvers(home, capsys, monkeypatch):
    body = ("JATS full text cached content. " * 30).strip()
    text_path(PLOS).parent.mkdir(parents=True, exist_ok=True)
    text_path(PLOS).write_text(body, encoding="utf-8")
    write_meta(
        PLOS,
        {
            "title": "XML Cached Paper",
            "resolver": "europepmc",
            "version": "publishedVersion",
            "license": "cc-by",
            "text_chars": len(body),
        },
    )

    assert not pdf_path(PLOS).exists()
    assert cache_ok(PLOS)

    def boom(*a, **k):
        raise AssertionError("Network should not be touched on cache hit")

    monkeypatch.setattr("papers.cli.europepmc_resolve", boom)
    monkeypatch.setattr("papers.cli.lookup", boom)

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "europepmc"
    assert rec["title"] == "XML Cached Paper"


def test_extract_jats_unit():
    full_xml = (FIXTURES / "epmc_fulltext.xml").read_text(encoding="utf-8")
    extracted = extract_jats(full_xml)
    assert "EPMC_FULLTEXT_MARKER" in extracted
    assert len(extracted.strip()) >= 500
    assert "Methods" in extracted
    # Acknowledgments section was skipped
    assert "Acknowledgments" not in extracted
    assert "should be skipped by the parser" not in extracted

    short_xml = (FIXTURES / "epmc_fulltext_short.xml").read_text(encoding="utf-8")
    short_extracted = extract_jats(short_xml)
    assert len(short_extracted.strip()) < 500
    assert "Brief Note" in short_extracted

    assert extract_jats("<invalid-xml>") == ""
    assert extract_jats("<article><front></front></article>") == ""
    assert extract_jats("") == ""


def test_write_jats_text_unit(tmp_path):
    full_xml = (FIXTURES / "epmc_fulltext.xml").read_text(encoding="utf-8")
    dest = tmp_path / "out_text.txt"
    chars = write_jats_text(full_xml, dest)
    assert chars >= 500
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == extract_jats(full_xml)


def test_openalex_hit_ok(home, capsys, monkeypatch):
    import papers.openalex

    oa_json = (FIXTURES / "openalex_oa.json").read_bytes()
    urls_called = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        urls_called.append(url)
        return io.BytesIO(oa_json)

    def fake_download_pdf(url, dest, mailto):
        write_pdf(dest, "OpenAlex readable paper content " * 30)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("papers.openalex.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "A mutation in the VPS33A gene", "PLoS ONE", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == PLOS
    assert rec["resolver"] == "openalex"
    assert rec["version"] == "publishedVersion"
    assert rec["license"] == "cc-by"
    assert rec["title"] == "A mutation in the VPS33A gene"
    assert text_path(PLOS).is_file()
    assert pdf_path(PLOS).is_file()
    assert len(urls_called) == 1
    assert "api.openalex.org/works/https://doi.org/10.1371%2Fjournal.pone.0000308" in urls_called[0]
    assert "mailto=" in urls_called[0]


def test_openalex_null_pdf_falls_through(home, capsys, monkeypatch):
    import papers.openalex

    oa_json = (FIXTURES / "openalex_null_pdf.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))
    monkeypatch.setattr(
        "papers.openalex.download_pdf",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,openalex"
    assert not pdf_path(CLOSED).exists()


def test_openalex_non_pdf_falls_through(home, capsys, monkeypatch):
    import papers.openalex

    oa_json = (FIXTURES / "openalex_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))

    def boom(url, dest, mailto):
        raise FetchError("not a PDF")

    monkeypatch.setattr("papers.openalex.download_pdf", boom)
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,openalex"
    assert not pdf_path(CLOSED).exists()


def test_openalex_cdn_url_never_fetched(home, capsys, monkeypatch):
    import papers.openalex

    cdn_json = (FIXTURES / "openalex_cdn.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(cdn_json))
    monkeypatch.setattr(
        "papers.openalex.download_pdf",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CDN URL must never be fetched")),
    )
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "CDN Paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,openalex"


def test_openalex_cdn_falls_back_to_other_location(home, capsys, monkeypatch):
    import papers.openalex

    payload = {
        "display_name": "Fallback paper",
        "publication_year": 2007,
        "primary_location": {"source": {"display_name": "PLoS ONE"}},
        "best_oa_location": {
            "pdf_url": "https://content.openalex.org/works/W1/pdf",
            "version": "publishedVersion",
            "license": "cc-by",
        },
        "locations": [
            {
                "pdf_url": "https://content.openalex.org/works/W1/pdf",
                "version": "publishedVersion",
            },
            {
                "pdf_url": "https://files.example.test/paper.pdf",
                "version": "acceptedVersion",
                "license": "cc-by",
            },
        ],
    }
    seen = []

    def fake_download_pdf(url, dest, mailto):
        seen.append(url)
        if "content.openalex.org" in url:
            raise AssertionError("CDN URL must never be fetched")
        write_pdf(dest, "OpenAlex fallback readable paper content " * 30)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr("papers.openalex.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Fallback paper", "PLoS ONE", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "openalex"
    assert rec["version"] == "acceptedVersion"
    assert seen == ["https://files.example.test/paper.pdf"]


def test_openalex_null_best_uses_locations_pdf(home, capsys, monkeypatch):
    import papers.openalex

    payload = {
        "display_name": "Alt loc paper",
        "best_oa_location": {"pdf_url": None},
        "locations": [
            {"pdf_url": None},
            {"pdf_url": "https://files.example.test/alt.pdf", "version": "publishedVersion", "license": "cc-by"},
        ],
    }

    def fake_download_pdf(url, dest, mailto):
        assert url == "https://files.example.test/alt.pdf"
        write_pdf(dest, "OpenAlex alt location readable paper content " * 30)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr("papers.openalex.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Alt loc paper", "PLoS ONE", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "openalex"


def test_openalex_short_pdf_is_unreadable(home, capsys, monkeypatch):
    import papers.openalex

    oa_json = (FIXTURES / "openalex_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))

    def fake_short_pdf(url, dest, mailto):
        write_pdf(dest, "short")

    monkeypatch.setattr("papers.openalex.download_pdf", fake_short_pdf)
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Short Paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "unreadable_pdf"
    assert rec["text_chars"] < 500
    assert pdf_path(CLOSED).is_file()


def test_biorxiv_preprint_ok(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.1101/2024.01.01.123456"
    urls_called = []

    def fake_download_pdf(url, dest, mailto):
        urls_called.append(url)
        write_pdf(dest, "bioRxiv preprint readable content " * 30)

    monkeypatch.setattr("papers.preprints.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "bioRxiv paper", "bioRxiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == doi
    assert rec["resolver"] == "preprint"
    assert rec["version"] == "preprint"
    assert urls_called == [f"https://www.biorxiv.org/content/{doi}v1.full.pdf"]


def test_medrxiv_preprint_fallback(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.1101/2024.01.01.123456"
    urls_called = []

    def fake_download_pdf(url, dest, mailto):
        urls_called.append(url)
        if "biorxiv" in url:
            raise FetchError("not a PDF")
        if "medrxiv" in url:
            write_pdf(dest, "medRxiv preprint readable content " * 30)

    monkeypatch.setattr("papers.preprints.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.preprints.time.sleep", lambda s: None)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "medRxiv paper", "medRxiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "preprint"
    # bioRxiv once, then medRxiv — do not retry the wrong host first
    assert urls_called == [
        f"https://www.biorxiv.org/content/{doi}v1.full.pdf",
        f"https://www.medrxiv.org/content/{doi}v1.full.pdf",
    ]


def test_preprint_retries_once_after_both_hosts_fail(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.1101/2024.01.01.654321"
    calls = []
    sleeps = []

    def fake_download_pdf(url, dest, mailto):
        calls.append(url)
        if len(calls) <= 2:
            raise FetchError("HTTP Error 403: Forbidden")
        write_pdf(dest, "medRxiv preprint readable content " * 30)

    monkeypatch.setattr("papers.preprints.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.preprints.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "retry paper", "bioRxiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert sleeps == [papers.preprints.RETRY_GAP_SEC]
    assert len(calls) == 3
    assert "biorxiv" in calls[0] and "medrxiv" in calls[1]
    assert calls[2] == calls[0]


def test_psyarxiv_preprint_ok(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.31234/osf.io/abcd1"
    urls_called = []

    def fake_download_pdf(url, dest, mailto):
        urls_called.append(url)
        write_pdf(dest, "PsyArXiv preprint readable content " * 30)

    monkeypatch.setattr("papers.preprints.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "PsyArXiv paper", "PsyArXiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == doi
    assert rec["resolver"] == "preprint"
    assert rec["version"] == "preprint"
    assert urls_called == ["https://osf.io/abcd1/download"]


def test_arxiv_preprint_ok(home, capsys, monkeypatch):
    import time
    import papers.preprints

    doi = "10.48550/arXiv.2301.00001"
    normalized_doi = "10.48550/arxiv.2301.00001"
    urls_called = []

    monkeypatch.setattr(time, "sleep", lambda s: None)

    def fake_download_pdf(url, dest, mailto):
        urls_called.append(url)
        write_pdf(dest, "arXiv preprint readable content " * 30)

    monkeypatch.setattr("papers.preprints.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "arXiv paper", "arXiv", 2023, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == normalized_doi
    assert rec["resolver"] == "preprint"
    assert rec["version"] == "preprint"
    assert urls_called == ["https://arxiv.org/pdf/2301.00001.pdf"]


def test_arxiv_rate_limit(monkeypatch):
    import time
    import papers.preprints

    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    papers.preprints._last_arxiv_request = time.time()
    papers.preprints._rate_limit_arxiv()
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= papers.preprints.ARXIV_GAP_SEC


def test_biorxiv_prefix_without_digit_not_treated_as_biorxiv(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.1101/gad.123456"
    monkeypatch.setattr(
        "papers.preprints.download_pdf",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "CSH Journal paper", "Genes & Dev", 2020, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"


def test_title_argument_crossref_hit(home, capsys, monkeypatch):
    seed_ok(PLOS)

    cr_json = (FIXTURES / "crossref_title.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(cr_json))

    code, out, err = run(capsys, ["get", "A mutation in the VPS33A gene"])
    assert code == 0
    assert err.strip() == f"resolved title -> {PLOS}"
    rec = json.loads(out)
    assert rec["status"] == "ok"
    assert rec["doi"] == PLOS


def test_title_argument_crossref_skips_peer_review(home, capsys, monkeypatch):
    """Crossref ranks 'Decision letter' peer-reviews above the article; skip them."""
    seed_ok(PLOS)
    cr = {
        "message": {
            "items": [
                {"DOI": "10.7554/elife.00461.010", "type": "peer-review",
                 "title": ["Decision letter: A mutation in the VPS33A gene"]},
                {"DOI": "10.3410/f.1", "type": "dataset",
                 "title": ["Faculty Opinions recommendation of A mutation"]},
                {"DOI": PLOS, "type": "journal-article",
                 "title": ["A mutation in the VPS33A gene"]},
            ]
        }
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(cr).encode()),
    )
    code, out, err = run(capsys, ["get", "A mutation in the VPS33A gene"])
    assert code == 0
    assert json.loads(out)["doi"] == PLOS


def test_title_argument_crossref_prefers_journal_over_nature_precedings(home, capsys, monkeypatch):
    """Crossref types Nature Precedings as journal-article and ranks it first."""
    seed_ok(PLOS)
    cr = {
        "message": {
            "items": [
                {"DOI": "10.1038/npre.2007.361.1", "type": "journal-article",
                 "title": ["Sharing detailed research data is associated with increased citation rate"],
                 "container-title": ["Nature Precedings"]},
                {"DOI": "10.1038/npre.2007.361", "type": "journal-article",
                 "title": ["Sharing detailed research data is associated with increased citation rate"],
                 "container-title": ["Nature Precedings"]},
                {"DOI": PLOS, "type": "journal-article",
                 "title": ["Sharing detailed research data is associated with increased citation rate"],
                 "container-title": ["PLoS ONE"],
                 "ISSN": ["1932-6203"]},
            ]
        }
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(cr).encode()),
    )
    code, out, err = run(
        capsys,
        ["get", "Sharing detailed research data is associated with increased citation rate"],
    )
    assert code == 0
    assert err.strip() == f"resolved title -> {PLOS}"
    assert json.loads(out)["doi"] == PLOS


def test_title_argument_crossref_skips_posted_content(home, capsys, monkeypatch):
    """Nature Precedings / preprint records rank above the journal article."""
    seed_ok(PLOS)
    cr = {
        "message": {
            "items": [
                {"DOI": "10.1038/npre.2007.361.1", "type": "posted-content",
                 "title": ["Sharing detailed research data is associated with increased citation rate"]},
                {"DOI": PLOS, "type": "journal-article",
                 "title": ["Sharing detailed research data is associated with increased citation rate"]},
            ]
        }
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(cr).encode()),
    )
    code, out, err = run(capsys, ["get", "Sharing detailed research data is associated with increased citation rate"])
    assert code == 0
    assert json.loads(out)["doi"] == PLOS


def test_title_argument_crossref_prefers_journal_article(home, capsys, monkeypatch):
    seed_ok(PLOS)
    cr = {
        "message": {
            "items": [
                {"DOI": "10.1000/chapter", "type": "book-chapter",
                 "title": ["Something else entirely"]},
                {"DOI": PLOS, "type": "journal-article",
                 "title": ["Also not an exact match but it is the article"]},
            ]
        }
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(cr).encode()),
    )
    code, out, _ = run(capsys, ["get", "not an exact title at all"])
    assert code == 0
    assert json.loads(out)["doi"] == PLOS


def test_title_argument_crossref_exact_title_beats_rank(home, capsys, monkeypatch):
    seed_ok(PLOS)
    cr = {
        "message": {
            "items": [
                {"DOI": "10.1000/other", "type": "book-chapter",
                 "title": ["Is a mutation in the VPS33A gene enough?"]},
                {"DOI": PLOS, "type": "journal-article",
                 "title": ["A mutation in the VPS33A gene"]},
            ]
        }
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(cr).encode()),
    )
    code, out, _ = run(capsys, ["get", "a mutation in the vps33a gene."])
    assert code == 0
    assert json.loads(out)["doi"] == PLOS


def test_title_argument_crossref_miss_exits_1(home, capsys, monkeypatch):
    cr_json = (FIXTURES / "crossref_empty.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(cr_json))

    code, out, err = run(capsys, ["get", "Unknown Nonexistent Paper Title"])
    assert code == 1
    rec = json.loads(out)
    assert rec == {"status": "no_doi", "agent_next": "notify_human"}


def test_not_oa_anywhere_full_tried_list(home, capsys, monkeypatch):
    import papers.openalex
    import papers.preprints

    doi = "10.1101/2024.01.01.999999"
    oa_json = (FIXTURES / "openalex_null_pdf.json").read_bytes()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)

    def preprint_fail(url, dest, mailto):
        raise FetchError("preprint download failed")

    monkeypatch.setattr("papers.preprints.download_pdf", preprint_fail)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "Preprint miss", "bioRxiv", 2024, None, None),
    )

    code1, out1, _ = run(capsys, ["get", doi])
    assert code1 == 2
    rec1 = json.loads(out1)
    assert rec1["status"] == "no_oa"
    assert rec1["tried"] == "europepmc,unpaywall,openalex,preprint"

    code2, out2, _ = run(capsys, ["get", doi])
    assert code2 == 2
    rec2 = json.loads(out2)
    assert rec2["status"] == "no_oa"
    assert rec2["tried"] == "europepmc,unpaywall,openalex,preprint"


def test_openalex_extraction_error_falls_through(home, capsys, monkeypatch):
    import papers.openalex

    oa_json = (FIXTURES / "openalex_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))
    monkeypatch.setattr("papers.openalex.download_pdf", lambda url, dest, mailto: write_pdf(dest, "test"))
    monkeypatch.setattr("papers.openalex.write_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt pdf")))
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,openalex"


def test_empty_psyarxiv_id_treated_as_preprint_miss(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.31234/osf.io/"
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "Empty PsyArXiv", "PsyArXiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall"


def test_empty_arxiv_id_treated_as_preprint_miss(home, capsys, monkeypatch):
    import papers.preprints

    doi = "10.48550/arxiv."
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "Empty arXiv", "arXiv", 2024, None, None),
    )

    code, out, _ = run(capsys, ["get", doi])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,preprint"


def test_crossref_malformed_json_list_returns_no_doi(home, capsys, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(b"[]"))

    code, out, err = run(capsys, ["get", "Some Paper Title"])
    assert code == 1
    assert "Traceback" not in err
    rec = json.loads(out)
    assert rec == {"status": "no_doi", "agent_next": "notify_human"}


def test_openalex_failure_after_download_cleans_up_files(home, capsys, monkeypatch):
    import papers.openalex
    from papers.cache import meta_path, pdf_path, text_path

    oa_json = (FIXTURES / "openalex_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(oa_json))
    monkeypatch.setattr("papers.openalex.download_pdf", lambda url, dest, mailto: write_pdf(dest, "test"))

    def fake_write_text(dest_pdf, dest_txt):
        dest_txt.parent.mkdir(parents=True, exist_ok=True)
        dest_txt.write_text("dummy text " * 100, encoding="utf-8")
        return 1100

    monkeypatch.setattr("papers.openalex.write_text", fake_write_text)
    monkeypatch.setattr("papers.openalex.write_meta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("meta write fail")))
    monkeypatch.setattr("papers.cli.openalex_resolve", papers.openalex.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert not pdf_path(CLOSED).exists()
    assert not text_path(CLOSED).exists()
    assert not meta_path(CLOSED).exists()


def test_near_preprint_prefixes_skip_shortcut(home, capsys, monkeypatch):
    import papers.preprints

    monkeypatch.setattr(
        "papers.preprints.download_pdf",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "Near prefix paper", "Test Journal", 2024, None, None),
    )

    for doi in ["10.31234/osf.iox", "10.48550/arxivx"]:
        code, out, _ = run(capsys, ["get", doi])
        rec = json.loads(out)
        assert code == 2
        assert rec["status"] == "no_oa"
        assert rec["tried"] == "europepmc,unpaywall"


def _core_search(*results):
    return {"totalHits": len(results), "results": list(results)}


def _core_wire(monkeypatch, discover: dict | None, key: str = "test-key"):
    import papers.core

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return io.BytesIO(json.dumps(discover).encode())

    def fake_download_pdf(url, dest, mailto):
        write_pdf(dest, "CORE repository readable paper content " * 30)

    monkeypatch.setenv("CORE_API_KEY", key)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("papers.core.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.core_resolve", papers.core.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "A mutation in the VPS33A gene", "PLoS ONE", 2007, None, None),
    )
    return calls


def test_core_hit_ok(home, capsys, monkeypatch):
    from papers.cache import read_meta

    calls = _core_wire(monkeypatch, _core_search(
        {"doi": "10.1136/bmj.n71", "title": "wrong record", "downloadUrl": "https://core.ac.uk/download/9.pdf"},
        {"doi": PLOS.upper(), "title": "Sharing detailed research data", "downloadUrl": "https://core.ac.uk/download/1.pdf"},
    ))
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "core"
    assert read_meta(PLOS).get("resolver") == "core"
    assert len(calls) == 1
    assert rec["title"] == "Sharing detailed research data"
    assert calls[0].full_url.startswith("https://api.core.ac.uk/v3/search/works/?q=doi%3A%22")
    assert calls[0].get_header("Authorization") == "Bearer test-key"


def test_core_falls_back_to_source_url_when_core_download_fails(home, capsys, monkeypatch):
    import papers.core

    _core_wire(monkeypatch, _core_search({
        "doi": PLOS, "title": "t",
        "downloadUrl": "https://core.ac.uk/download/1.pdf",
        "sourceFulltextUrls": ["http://openaccess.example.ac.uk/1/paper.pdf"],
    }))
    tried_urls = []

    def flaky_download(url, dest, mailto):
        tried_urls.append(url)
        if "core.ac.uk" in url:
            raise FetchError("HTTP Error 400: Bad Request")
        write_pdf(dest, "repository copy readable content " * 30)

    monkeypatch.setattr("papers.core.download_pdf", flaky_download)
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["resolver"] == "core"
    assert tried_urls == ["https://core.ac.uk/download/1.pdf", "http://openaccess.example.ac.uk/1/paper.pdf"]


def test_core_no_key_skipped_not_in_tried(home, capsys, monkeypatch):
    calls = _core_wire(monkeypatch, _core_search({"doi": PLOS, "downloadUrl": "https://core.ac.uk/download/1.pdf"}), key="")
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert "core" not in rec["tried"].split(",")
    assert calls == []


def test_core_null_link_falls_through_to_queue(home, capsys, monkeypatch):
    _core_wire(monkeypatch, _core_search({"doi": PLOS, "downloadUrl": "", "sourceFulltextUrls": []}))
    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"].split(",")[-1] == "core"


def test_semanticscholar_hit_ok(home, capsys, monkeypatch):
    import papers.semanticscholar
    from papers.cache import read_meta

    s2_json = (FIXTURES / "s2_oa.json").read_bytes()
    urls_called = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        urls_called.append(url)
        return io.BytesIO(s2_json)

    def fake_download_pdf(url, dest, mailto):
        write_pdf(dest, "Semantic Scholar readable paper content " * 30)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("papers.semanticscholar.download_pdf", fake_download_pdf)
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "A mutation in the VPS33A gene", "PLoS ONE", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["doi"] == PLOS
    assert rec["resolver"] == "semanticscholar"
    assert rec["license"] == "CC-BY"
    assert rec["title"] == "A mutation in the VPS33A gene"
    assert text_path(PLOS).is_file()
    assert pdf_path(PLOS).is_file()
    assert read_meta(PLOS).get("resolver") == "semanticscholar"
    assert len(urls_called) == 1
    assert "api.semanticscholar.org/graph/v1/paper/DOI:10.1371/journal.pone.0000308" in urls_called[0]
    assert "fields=title,openAccessPdf,externalIds" in urls_called[0]


def test_semanticscholar_null_pdf_falls_through(home, capsys, monkeypatch):
    import papers.semanticscholar

    s2_json = (FIXTURES / "s2_null_pdf.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(s2_json))
    monkeypatch.setattr(
        "papers.semanticscholar.download_pdf",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,semanticscholar"
    assert not pdf_path(CLOSED).exists()


def test_semanticscholar_non_pdf_falls_through(home, capsys, monkeypatch):
    import papers.semanticscholar

    s2_json = (FIXTURES / "s2_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(s2_json))

    def boom(url, dest, mailto):
        raise FetchError("not a PDF")

    monkeypatch.setattr("papers.semanticscholar.download_pdf", boom)
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "no_oa"
    assert rec["tried"] == "europepmc,unpaywall,semanticscholar"
    assert not pdf_path(CLOSED).exists()


def test_semanticscholar_short_pdf_is_unreadable(home, capsys, monkeypatch):
    import papers.semanticscholar

    s2_json = (FIXTURES / "s2_oa.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(s2_json))

    def fake_short_pdf(url, dest, mailto):
        write_pdf(dest, "short")

    monkeypatch.setattr("papers.semanticscholar.download_pdf", fake_short_pdf)
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Short Paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    rec = json.loads(out)
    assert code == 2
    assert rec["status"] == "unreadable_pdf"
    assert rec["text_chars"] < 500
    assert pdf_path(CLOSED).is_file()


def test_semanticscholar_429_twice_skips_process_and_sleeps_once(home, capsys, monkeypatch):
    import papers.preprints
    import papers.semanticscholar

    sleeps = []
    monkeypatch.setattr("papers.semanticscholar._sleep", lambda s: sleeps.append(s))

    doi = "10.1101/2024.01.01.123456"

    def fake_429_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_429_urlopen)
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr("papers.cli.preprint_resolve", papers.preprints.resolve)

    def preprint_fail(url, dest, mailto):
        raise FetchError("preprint download failed")

    monkeypatch.setattr("papers.preprints.download_pdf", preprint_fail)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "429 test paper", "bioRxiv", 2024, None, None),
    )

    code1, out1, _ = run(capsys, ["get", doi])
    assert code1 == 2
    rec1 = json.loads(out1)
    assert rec1["status"] == "no_oa"
    assert "semanticscholar" not in rec1["tried"].split(",")
    assert sleeps == [30]

    # Second get in same process: s2_resolve skips immediately without HTTP or sleep
    def s2_should_not_run(req, timeout=None):
        raise AssertionError("urlopen should not be called when process skip is active")

    monkeypatch.setattr(urllib.request, "urlopen", s2_should_not_run)

    code2, out2, _ = run(capsys, ["get", doi])
    assert code2 == 2
    rec2 = json.loads(out2)
    assert rec2["status"] == "no_oa"
    assert "semanticscholar" not in rec2["tried"].split(",")
    assert sleeps == [30]


def test_semanticscholar_api_key_header(home, monkeypatch):
    import papers.semanticscholar

    s2_json = (FIXTURES / "s2_null_pdf.json").read_bytes()
    captured_requests = []

    def fake_urlopen(req, timeout=None):
        captured_requests.append(req)
        return io.BytesIO(s2_json)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # Phase 1: env unset -> no x-api-key header
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    papers.semanticscholar.resolve(CLOSED, "tester@example.test")
    assert len(captured_requests) == 1
    req1 = captured_requests[0]
    header_keys = [k.lower() for k in req1.headers.keys()]
    assert "x-api-key" not in header_keys

    # Phase 2: env set -> x-api-key header present with value
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-secret-key")
    papers.semanticscholar.resolve(CLOSED, "tester@example.test")
    assert len(captured_requests) == 2
    req2 = captured_requests[1]
    # urllib Request stores headers capitalized or lowercased
    s2_key_header = None
    for k, v in req2.headers.items():
        if k.lower() == "x-api-key":
            s2_key_header = v
    assert s2_key_header == "test-secret-key"


def test_status_empty_cache(home, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched by status")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)

    code1, out1, _ = run(capsys, ["status"])
    assert code1 == 0
    rec1 = json.loads(out1)

    code2, out2, _ = run(capsys, ["status", "--json"])
    assert code2 == 0
    rec2 = json.loads(out2)

    assert rec1 == rec2
    assert rec1["cached"] == {"count": 0, "chars": 0}
    assert rec1["unreadable"] == {"count": 0}
    assert rec1["cache_root"] == str(cache_root())
    assert rec1["mailto_set"] is True
    assert rec1["s2_key_set"] is False
    assert "queue" not in rec1
    assert "queue_path" not in rec1


def test_status_seeded_cache(home, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched by status")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-key")

    # 1. Normal cached paper with pdf + text
    seed_ok(PLOS, title="PLOS Paper")
    text1_len = text_chars(PLOS)

    # 2. XML-only cached paper (no PDF)
    xml_doi = "10.1000/xml_paper"
    xml_text = ("XML body text content. " * 40).strip()
    text_path(xml_doi).parent.mkdir(parents=True, exist_ok=True)
    text_path(xml_doi).write_text(xml_text, encoding="utf-8")
    write_meta(xml_doi, {"title": "XML Paper", "resolver": "europepmc", "text_chars": len(xml_text)})
    text2_len = len(xml_text)

    # 3. Unreadable paper with PDF and short text
    unread_doi = CLOSED
    unread_pdf = pdf_path(unread_doi)
    unread_pdf.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(unread_pdf, "Short")
    text_path(unread_doi).write_text("Short", encoding="utf-8")
    write_meta(unread_doi, {"title": "Unreadable Paper", "text_chars": 5})

    code, out, _ = run(capsys, ["status"])
    assert code == 0
    rec = json.loads(out)

    assert rec["cached"] == {"count": 2, "chars": text1_len + text2_len}
    assert rec["unreadable"] == {"count": 1}
    assert rec["cache_root"] == str(cache_root())
    assert rec["mailto_set"] is True
    assert rec["s2_key_set"] is True
    assert "queue" not in rec
    assert "queue_path" not in rec


def test_semanticscholar_non_string_title_coerced_to_empty_string(home, capsys, monkeypatch):
    import papers.semanticscholar
    from papers.cache import read_meta

    data = json.loads((FIXTURES / "s2_oa.json").read_text(encoding="utf-8"))
    data["title"] = 12345
    s2_json = json.dumps(data).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(s2_json))
    monkeypatch.setattr(
        "papers.semanticscholar.download_pdf",
        lambda url, dest, mailto: write_pdf(dest, "Semantic Scholar readable paper content " * 30),
    )
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Original title", "PLoS ONE", 2007, None, None),
    )

    code, out, _ = run(capsys, ["get", PLOS])
    rec = json.loads(out)
    assert code == 0
    assert rec["status"] == "ok"
    assert rec["title"] == ""
    assert isinstance(read_meta(PLOS)["title"], str)
    assert read_meta(PLOS)["title"] == ""


def test_status_corrupt_files_exits_0(home, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched by status")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    # Corrupt text.txt in a cache subfolder
    corrupt_doi = "10.1000/corrupt_paper"
    text_path(corrupt_doi).parent.mkdir(parents=True, exist_ok=True)
    text_path(corrupt_doi).write_bytes(b"\xff\xfe\x00\x00binary text\x80\x81")

    code, out, _ = run(capsys, ["status"])
    assert code == 0
    rec = json.loads(out)
    assert isinstance(rec, dict)
    assert rec["cached"]["count"] == 0
    assert "queue" not in rec


def test_uspmc_never_requests_direct_pdf(home, capsys, monkeypatch):
    """pmc.ncbi.nlm.nih.gov/articles/{PMCID}/pdf/ is a proof-of-work page.
    The resolver must not download it; AWS is the first PDF step."""
    import papers.uspmc

    monkeypatch.setattr("papers.cli.uspmc_resolve", papers.uspmc.resolve)

    def boom(*a, **k):
        raise AssertionError("network / unpaywall must not be called")

    monkeypatch.setattr("papers.cli.lookup", boom)

    def fake_fetch_bytes(url, mailto):
        if "idconv" in url:
            return (FIXTURES / "idconv_hit.json").read_bytes()
        if url.startswith("https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC7102627."):
            return (FIXTURES / "aws_listing_pmc7102627.xml").read_bytes()
        raise AssertionError(f"unexpected url: {url}")

    def fake_download_pdf(url, dest, mailto):
        if "/articles/" in url and url.rstrip("/").endswith("/pdf"):
            raise AssertionError(f"direct PMC /pdf/ must not be requested: {url}")
        if url == "https://pmc-oa-opendata.s3.amazonaws.com/PMC7102627.1/PMC7102627.1.pdf":
            return write_pdf(dest, "USPMC aws bucket readable content " * 30)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("papers.uspmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.uspmc.download_pdf", fake_download_pdf)

    code, out, _ = run(capsys, ["get", PLOS])
    assert code == 0
    rec = json.loads(out)
    assert rec["status"] == "ok"
    assert rec["resolver"] == "uspmc"
    assert rec["read"] == str(text_path(PLOS).resolve())
    assert rec["text_chars"] >= 500
    assert rec["title"] == "USPMC fixture title"

    meta = read_meta(PLOS)
    assert meta["pmcid"] == "PMC7102627"
    assert meta["resolver"] == "uspmc"
    assert meta["title"] == "USPMC fixture title"
    assert meta["journal"] == "Cell"
    assert meta["year"] == 2020


def test_extract_html_keeps_abstract_and_body_only():
    page = (FIXTURES / "pmc_article_manuscript.html").read_text(encoding="utf-8")
    text = extract_html(page)
    assert "Fifty-seven humans were fed a low choline diet" in text  # abstract
    assert "18 of the 23 (78%) carriers" in text  # body
    assert "rs7946" in text  # table cells
    # dropped: page chrome, keywords, acknowledgments, references
    for gone in ("official website", "Search in PMC", "Keywords:", "DK55865", "Zeisel SH. Choline", "Similar articles", "Vulnerability"):
        assert gone not in text, gone
    assert extract_html("<html><body><p>Sign in to view this article</p></body></html>") == ""
    assert extract_html("") == ""


def test_uspmc_author_manuscript_reads_article_html(home, capsys, monkeypatch):
    """No PDF anywhere (direct link challenged, not in the OA bucket): the
    resolver reads the PMC article page and writes text.txt without a PDF."""
    import papers.uspmc

    monkeypatch.setattr("papers.cli.uspmc_resolve", papers.uspmc.resolve)

    def boom(*a, **k):
        raise AssertionError("network / unpaywall must not be called")

    monkeypatch.setattr("papers.cli.lookup", boom)

    def fake_fetch_bytes(url, mailto):
        if "idconv" in url:
            return (FIXTURES / "idconv_hit.json").read_bytes()
        if url.startswith(papers.uspmc.AWS_BUCKET):
            return b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><KeyCount>0</KeyCount></ListBucketResult>'
        if url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC7102627/":
            return (FIXTURES / "pmc_article_manuscript.html").read_bytes()
        raise AssertionError(f"unexpected url: {url}")

    def fake_download_pdf(url, dest, mailto):
        raise AssertionError(f"download_pdf must not be called: {url}")

    monkeypatch.setattr("papers.uspmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.uspmc.download_pdf", fake_download_pdf)

    code, out, _ = run(capsys, ["get", PLOS])
    assert code == 0
    rec = json.loads(out)
    assert rec["status"] == "ok"
    assert rec["resolver"] == "uspmc"
    assert rec["text_chars"] >= 500
    assert not pdf_path(PLOS).exists()
    text = text_path(PLOS).read_text(encoding="utf-8")
    assert "18 of the 23 (78%) carriers" in text
    assert "Zeisel SH. Choline" not in text
    meta = read_meta(PLOS)
    assert meta["pmcid"] == "PMC7102627"
    assert meta["version"] == "authorManuscript"


def test_uspmc_idconv_no_pmcid_falls_through(home, capsys, monkeypatch):
    import papers.uspmc

    monkeypatch.setattr("papers.cli.uspmc_resolve", papers.uspmc.resolve)

    def fake_fetch_bytes(url, mailto):
        if "idconv" in url:
            return (FIXTURES / "idconv_miss.json").read_bytes()
        raise AssertionError(f"unexpected url: {url}")

    def boom_download(*a, **k):
        raise AssertionError("download_pdf must not be called")

    monkeypatch.setattr("papers.uspmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.uspmc.download_pdf", boom_download)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    assert code == 2
    rec = json.loads(out)
    assert rec["status"] == "no_oa"
    assert "uspmc" in rec["tried"].split(",")
    assert rec["tried"] == "europepmc,uspmc,unpaywall"
    assert not pdf_path(CLOSED).exists()
    assert not text_path(CLOSED).exists()


def test_uspmc_idconv_http_500_falls_through(home, capsys, monkeypatch):
    import papers.uspmc

    monkeypatch.setattr("papers.cli.uspmc_resolve", papers.uspmc.resolve)

    def fake_fetch_bytes(url, mailto):
        raise FetchError("http 500")

    monkeypatch.setattr("papers.uspmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )

    code, out, _ = run(capsys, ["get", CLOSED])
    assert code == 2
    rec = json.loads(out)
    assert rec["status"] == "no_oa"
    assert "uspmc" in rec["tried"].split(",")
    assert rec["tried"] == "europepmc,uspmc,unpaywall"
    assert not pdf_path(CLOSED).exists()
    assert not text_path(CLOSED).exists()


def test_uspmc_metadata_coercion(home, capsys, monkeypatch):
    import papers.uspmc

    monkeypatch.setattr("papers.cli.uspmc_resolve", papers.uspmc.resolve)
    monkeypatch.setattr("papers.cli.lookup", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    data = {
        "status": "ok",
        "records": [
            {
                "pmcid": "PMC7102627",
                "doi": PLOS,
                "title": 99999,
                "journal": 88888,
                "year": "2020 (online)",
            }
        ],
    }

    def fake_fetch_bytes(url, mailto):
        if "idconv" in url:
            return json.dumps(data).encode("utf-8")
        if url.startswith("https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC7102627."):
            return (FIXTURES / "aws_listing_pmc7102627.xml").read_bytes()
        raise AssertionError(f"unexpected url: {url}")

    def fake_download_pdf(url, dest, mailto):
        if url == "https://pmc-oa-opendata.s3.amazonaws.com/PMC7102627.1/PMC7102627.1.pdf":
            return write_pdf(dest, "USPMC aws bucket readable content " * 30)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("papers.uspmc.fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr("papers.uspmc.download_pdf", fake_download_pdf)

    code, out, _ = run(capsys, ["get", PLOS])
    assert code == 0
    rec = json.loads(out)
    assert rec["status"] == "ok"
    assert rec["title"] == "99999"

    meta = read_meta(PLOS)
    assert meta["title"] == "99999"
    assert meta["journal"] == "88888"
    assert meta["year"] == 2020


def test_aws_pdf_key_picks_newest_version():
    from papers.uspmc import aws_pdf_key

    xml = (
        b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<Contents><Key>PMC1.1/PMC1.1.pdf</Key></Contents>"
        b"<Contents><Key>PMC1.3/PMC1.3.pdf</Key></Contents>"
        b"<Contents><Key>PMC1.3/fig1.jpg</Key></Contents>"
        b"<Contents><Key>PMC12.1/PMC12.1.pdf</Key></Contents>"
        b"</ListBucketResult>"
    )
    assert aws_pdf_key(xml, "PMC1") == "PMC1.3/PMC1.3.pdf"
    assert aws_pdf_key(b"<ListBucketResult/>", "PMC1") is None
    assert aws_pdf_key(b"not xml", "PMC1") is None


def test_get_batch_stdin_three_dois_in_order(home, capsys, monkeypatch):
    dois = ["10.1000/batch-a", "10.1000/batch-b", "10.1000/batch-c"]
    for d in dois:
        seed_ok(d, title=f"Paper {d}")

    def boom(*a, **k):
        raise AssertionError("network should not run")

    monkeypatch.setattr("papers.cli.lookup", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join([dois[0], "", dois[1], "  ", dois[2]]) + "\n"))
    code, out, err = run(capsys, ["get", "-"])
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 3
    recs = [json.loads(ln) for ln in lines]
    assert [r["doi"] for r in recs] == dois
    assert all(r["status"] == "ok" for r in recs)
    # each line is exactly what a single get prints
    _, single, _ = run(capsys, ["get", dois[1]])
    assert lines[1] == single.rstrip("\n")


def test_get_batch_positional_mixed_exits_2(home, capsys, monkeypatch):
    seed_ok(PLOS)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda doi, mailto: Lookup(False, None, "Closed paper", "JAMA", 2018, None, None),
    )
    code, out, _ = run(capsys, ["get", PLOS, CLOSED])
    assert code == 2
    recs = [json.loads(ln) for ln in out.splitlines()]
    assert [r["status"] for r in recs] == ["ok", "no_oa"]
    assert [r["doi"] for r in recs] == [PLOS, CLOSED]


def test_get_batch_title_on_stdin_prints_resolved(home, capsys, monkeypatch):
    seed_ok(PLOS)
    cr_json = (FIXTURES / "crossref_title.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(cr_json))
    monkeypatch.setattr("sys.stdin", io.StringIO("A mutation in the VPS33A gene\n"))
    code, out, err = run(capsys, ["get", "-"])
    assert code == 0
    assert err.strip() == f"resolved title -> {PLOS}"
    assert json.loads(out)["doi"] == PLOS


def test_get_stdin_empty_is_config_error(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n"))
    code, out, err = run(capsys, ["get", "-"])
    assert code == 1
    assert json.loads(out)["status"] == "config_error"


def test_get_batch_s2_skip_holds_across_batch(home, capsys, monkeypatch):
    import papers.semanticscholar

    sleeps = []
    calls = []
    monkeypatch.setattr("papers.semanticscholar._sleep", lambda s: sleeps.append(s))

    def fake_429_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_429_urlopen)
    monkeypatch.setattr("papers.cli.s2_resolve", papers.semanticscholar.resolve)
    monkeypatch.setattr(
        "papers.cli.lookup",
        lambda d, mailto: Lookup(False, None, "429 test paper", "JAMA", 2018, None, None),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("10.1000/s2-one\n10.1000/s2-two\n"))
    code, out, _ = run(capsys, ["get", "-"])
    assert code == 2
    recs = [json.loads(ln) for ln in out.splitlines()]
    assert [r["doi"] for r in recs] == ["10.1000/s2-one", "10.1000/s2-two"]
    assert sleeps == [30]  # one sleep for the whole batch, not one per DOI
    assert len(calls) == 2  # first try + retry on DOI one; DOI two never hits S2
    assert papers.semanticscholar._skip_for_process is True
