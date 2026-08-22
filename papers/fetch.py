"""Download a PDF after an SSRF gate. Reuse this on every later resolver."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from papers.extract import looks_like_pdf

TIMEOUT_SEC = 30
MAX_PDF_BYTES = 50 * 1024 * 1024
UA_BASE = "paperfetch-oa/0.1 (+https://github.com/bartholomewtj/paperfetch-oa)"

_METADATA_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
}


class FetchError(Exception):
    pass


class UnsafeUrl(FetchError):
    pass


def user_agent(mailto: str) -> str:
    return f"{UA_BASE} (mailto:{mailto})"


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_url_safe(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl(url)
    host = parsed.hostname
    if not host:
        raise UnsafeUrl(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in (80, 443):
        raise UnsafeUrl(url)
    host_l = host.lower().rstrip(".")
    if host_l in _METADATA_HOSTS or host_l.endswith(".localhost"):
        raise UnsafeUrl(url)
    try:
        literal = ipaddress.ip_address(host_l)
    except ValueError:
        literal = None
    if literal is not None:
        if _bad_ip(literal):
            raise UnsafeUrl(url)
        return
    try:
        infos = socket.getaddrinfo(host_l, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl(url) from exc
    if not infos:
        raise UnsafeUrl(url)
    for info in infos:
        addr = info[4][0]
        if _bad_ip(ipaddress.ip_address(addr)):
            raise UnsafeUrl(url)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_url_safe(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, mailto: str) -> bytes:
    assert_url_safe(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(mailto)},
    )
    opener = urllib.request.build_opener(SafeRedirectHandler)
    try:
        with opener.open(req, timeout=TIMEOUT_SEC) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_PDF_BYTES:
                raise FetchError("too large")
            got = 0
            chunks: list[bytes] = []
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_PDF_BYTES:
                    raise FetchError("too large")
                chunks.append(chunk)
            return b"".join(chunks)
    except (UnsafeUrl, FetchError):
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(str(exc)) from exc


def download_pdf(url: str, dest: Path, mailto: str) -> None:
    assert_url_safe(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(mailto)},
    )
    opener = urllib.request.build_opener(SafeRedirectHandler)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    try:
        with opener.open(req, timeout=TIMEOUT_SEC) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_PDF_BYTES:
                raise FetchError("too large")
            got = 0
            first = b""
            with tmp.open("wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > MAX_PDF_BYTES:
                        raise FetchError("too large")
                    if not first:
                        first = chunk
                        if not looks_like_pdf(first):
                            raise FetchError("not a PDF")
                    out.write(chunk)
        if got == 0 or not looks_like_pdf(first):
            raise FetchError("not a PDF")
        tmp.replace(dest)
    except (UnsafeUrl, FetchError):
        _drop(tmp)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _drop(tmp)
        raise FetchError(str(exc)) from exc
    finally:
        if tmp.exists() and not dest.exists():
            _drop(tmp)


def _drop(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
