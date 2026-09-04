"""Offline tests for the bioRxiv / medRxiv API resolver (papers/biorxiv.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import papers.biorxiv
from papers.biorxiv import resolve
from papers.cache import meta_path, paper_dir, pdf_path, read_meta, text_path
from papers.fetch import FetchError

FIXTURES = Path(__file__).parent / "fixtures"
DOI = "10.1101/2020.03.24.20042937"
EMPTY = b'{"messages":[{"status":"no posts found"}], "collection":[]}'
MAILTO = "tester@example.test"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _api_stub(monkeypatch, by_server: dict[str, bytes], calls: list[str]):
    def fake_fetch_bytes(url, mailto):
        calls.append(url)
        for server, payload in by_server.items():
            if url.startswith(f"https://api.biorxiv.org/details/{server}/"):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr("papers.biorxiv.fetch_bytes", fake_fetch_bytes)


def _pdf_stub(monkeypatch, fixture: str, downloads: list[str]):
    def fake_download_pdf(url, dest, mailto):
        downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture, dest)

    monkeypatch.setattr("papers.biorxiv.download_pdf", fake_download_pdf)


def test_biorxiv_success(home, monkeypatch):
    calls: list[str] = []
    downloads: list[str] = []
    _api_stub(monkeypatch, {"biorxiv": (FIXTURES / "biorxiv_details_response.json").read_bytes()}, calls)
    _pdf_stub(monkeypatch, "biorxiv_sample.pdf", downloads)

    assert resolve(DOI, MAILTO) is True

    # Latest version wins: the fixture lists v1 and v2.
    assert downloads == [f"https://www.biorxiv.org/content/{DOI}v2.full.pdf"]
    assert calls == [f"https://api.biorxiv.org/details/biorxiv/{DOI}"]
    assert text_path(DOI).is_file()
    assert pdf_path(DOI).is_file()
    meta = read_meta(DOI)
    assert meta["resolver"] == "biorxiv"
    assert meta["version"] == "v2"
    assert meta["server"] == "biorxiv"
    assert meta["year"] == 2020
    assert meta["license"] == "cc_by_nd"
    assert meta["title"].startswith("Correlation between universal BCG")


def test_medrxiv_fallback(home, monkeypatch):
    calls: list[str] = []
    downloads: list[str] = []
    _api_stub(
        monkeypatch,
        {"biorxiv": EMPTY, "medrxiv": (FIXTURES / "medrxiv_details_response.json").read_bytes()},
        calls,
    )
    _pdf_stub(monkeypatch, "biorxiv_sample.pdf", downloads)

    assert resolve(DOI, MAILTO) is True

    assert calls == [
        f"https://api.biorxiv.org/details/biorxiv/{DOI}",
        f"https://api.biorxiv.org/details/medrxiv/{DOI}",
    ]
    assert downloads == [f"https://www.medrxiv.org/content/{DOI}v2.full.pdf"]
    meta = read_meta(DOI)
    assert meta["resolver"] == "biorxiv"
    assert meta["server"] == "medrxiv"
    assert meta["version"] == "v2"


def test_biorxiv_text_under_floor(home, monkeypatch):
    calls: list[str] = []
    downloads: list[str] = []
    _api_stub(monkeypatch, {"biorxiv": (FIXTURES / "biorxiv_details_response.json").read_bytes()}, calls)
    _pdf_stub(monkeypatch, "biorxiv_short.pdf", downloads)

    assert resolve(DOI, MAILTO) is False

    # Nothing left behind, so the next resolver starts clean.
    assert len(downloads) == 1
    assert not pdf_path(DOI).exists()
    assert not text_path(DOI).exists()
    assert not meta_path(DOI).exists()


def test_non_biorxiv_doi_skipped(home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call on a non-preprint DOI")

    monkeypatch.setattr("papers.biorxiv.fetch_bytes", boom)
    monkeypatch.setattr("papers.biorxiv.download_pdf", boom)

    assert resolve("10.1371/journal.pone.0000308", MAILTO) is None
    # The prefix must be followed by a digit, like preprints.py.
    assert resolve("10.1101/gr.123456.117", MAILTO) is None
    assert not paper_dir(DOI).exists()


def test_api_error_fallthrough(home, monkeypatch):
    downloads: list[str] = []
    _pdf_stub(monkeypatch, "biorxiv_sample.pdf", downloads)

    # HTTP 500 on both servers.
    calls: list[str] = []
    _api_stub(monkeypatch, {"biorxiv": FetchError("HTTP Error 500"), "medrxiv": FetchError("HTTP Error 500")}, calls)
    assert resolve(DOI, MAILTO) is False
    assert len(calls) == 2

    # Timeout.
    calls.clear()
    _api_stub(monkeypatch, {"biorxiv": TimeoutError("timed out"), "medrxiv": TimeoutError("timed out")}, calls)
    assert resolve(DOI, MAILTO) is False

    # Garbage body.
    _api_stub(monkeypatch, {"biorxiv": b"<html>oops</html>", "medrxiv": b"{}"}, calls)
    assert resolve(DOI, MAILTO) is False

    # PDF download blocked (Cloudflare 403) after a good API answer: one
    # retry after a gap, then give up cleanly.
    _api_stub(monkeypatch, {"biorxiv": (FIXTURES / "biorxiv_details_response.json").read_bytes()}, calls)
    attempts: list[str] = []
    sleeps: list[float] = []

    def blocked(url, dest, mailto):
        attempts.append(url)
        raise FetchError("HTTP Error 403")

    monkeypatch.setattr("papers.biorxiv.download_pdf", blocked)
    monkeypatch.setattr("papers.biorxiv.time.sleep", lambda s: sleeps.append(s))
    assert resolve(DOI, MAILTO) is False
    assert len(attempts) == 2
    assert sleeps == [papers.biorxiv.RETRY_GAP_SEC]

    assert downloads == []
    assert not paper_dir(DOI).exists() or not any(paper_dir(DOI).iterdir())


def test_pdf_retry_succeeds_after_one_403(home, monkeypatch):
    calls: list[str] = []
    attempts: list[str] = []
    _api_stub(monkeypatch, {"biorxiv": (FIXTURES / "biorxiv_details_response.json").read_bytes()}, calls)

    def flaky(url, dest, mailto):
        attempts.append(url)
        if len(attempts) == 1:
            raise FetchError("HTTP Error 403")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / "biorxiv_sample.pdf", dest)

    monkeypatch.setattr("papers.biorxiv.download_pdf", flaky)
    monkeypatch.setattr("papers.biorxiv.time.sleep", lambda s: None)
    assert resolve(DOI, MAILTO) is True
    assert len(attempts) == 2
    assert read_meta(DOI)["resolver"] == "biorxiv"


def test_parse_version_accepts_v_prefix():
    assert papers.biorxiv._parse_version("2") == 2
    assert papers.biorxiv._parse_version("v3") == 3
    assert papers.biorxiv._parse_version(None) is None
    assert papers.biorxiv._parse_version("") is None
